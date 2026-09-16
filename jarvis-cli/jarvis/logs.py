"""Conversation-scoped logs of the raw traffic between the model and the
Jarvis backend: every HTTP request Jarvis sends to a provider, every
response it gets back, and every tool call Jarvis runs in between.

Storage mirrors conversations.py on purpose — one append-only file per
conversation, under ``~/.jarvis/logs/<conv_id>.jsonl`` — so logs persist
across restarts and are naturally "conversation based": deleting a
conversation's log never touches another conversation's, and there's
nothing to migrate if conversations.py's own format changes.

JSON Lines (one compact JSON object per line) rather than one big JSON
array, so appending never requires reading/rewriting the whole file and a
crash mid-write only ever corrupts the last, incomplete line.
"""

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import conversations

JARVIS_DIR = Path.home() / ".jarvis"
LOG_DIR = JARVIS_DIR / "logs"
ENCODING = "utf-8"

MAX_LINE_CHARS = 20000  # a single runaway payload (huge tool result, etc.)
                        # shouldn't be able to blow up a log file or the UI
                        # rendering it; truncate defensively, note that we did.

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _log_path(conv_id):
    return LOG_DIR / f"{conv_id}.jsonl"


def _safe_json(obj):
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(obj), ensure_ascii=False)
    if len(text) > MAX_LINE_CHARS:
        text = text[:MAX_LINE_CHARS] + "...(truncated)"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"truncated": True, "preview": text}
    return obj


def log(conv_id, direction, data, provider=None, round_num=None):
    """Append one entry to conv_id's log. Never raises — a logging failure
    must never break an actual ask.

    direction: one of "request" | "response" | "tool_call" | "tool_result"
               | "error" | "info" — free-form label shown in the UI.
    data: any JSON-serializable payload (the raw request body, the raw
          response body, tool name+arguments, etc).
    """
    if not conversations.is_valid_id(conv_id):
        return
    entry = {
        "ts": _now(),
        "direction": direction,
        "provider": provider,
        "round": round_num,
        # Who drove this entry: "" (a person typing), "scheduler", "discord",
        # "instagram". Read from the environment rather than threaded through
        # every call site because the thing that knows the answer is whoever
        # *launched the process* — the scheduler spawning a job, or a chat
        # gateway handling a message — and that is several layers above the
        # dozen places inside ai_client/ai_providers that call log().
        #
        # Entry-level rather than conversation-level on purpose: a scheduled
        # job runs inside whatever conversation created it, so the same
        # conversation legitimately holds both hand-typed and job-driven
        # entries and labelling the conversation would mislabel half of them.
        "source": (os.environ.get("JARVIS_LOG_SOURCE") or "").strip()[:32],
        "data": _safe_json(data),
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _log_path(conv_id).open("a", encoding=ENCODING) as f:
                f.write(line + "\n")
    except OSError as e:
        print(f"Warning: couldn't write log for {conv_id}: {e}", file=sys.stderr)


def has_log(conv_id):
    return _log_path(conv_id).exists()


def list_logged_conversations():
    """Every conversation that has at least one log entry, newest first,
    enriched with title/updated_at from conversations.py's own index so the
    UI can show something meaningful instead of a bare id. Includes
    conversations no longer in the index (e.g. deleted) under their raw id,
    so their logs are still reachable rather than orphaned."""
    if not LOG_DIR.exists():
        return []
    index_by_id = {it["id"]: it for it in conversations.list_conversations()}
    items = []
    for path in LOG_DIR.glob("*.jsonl"):
        conv_id = path.stem
        if not conversations.is_valid_id(conv_id):
            continue
        meta = index_by_id.get(conv_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        items.append({
            "id": conv_id,
            "title": (meta or {}).get("title") or "(deleted conversation)",
            "updated_at": (meta or {}).get("updated_at"),
            "exists": meta is not None,
            # Label data for the Logs viewer. A conversation started by the
            # Discord bot, by Instagram, or by a scheduled job is not
            # something the user typed, and the viewer should say so rather
            # than presenting all three as indistinguishable chat history.
            "origin": (meta or {}).get("origin") or "",
            "origin_detail": (meta or {}).get("origin_detail") or "",
            "_mtime": mtime,
        })
    items.sort(key=lambda it: it["_mtime"], reverse=True)
    for it in items:
        it.pop("_mtime", None)
    return items


def read_entries(conv_id, limit=None):
    """All log entries for conv_id, oldest first. limit, if given, returns
    only the last `limit` entries (still oldest-first order)."""
    path = _log_path(conv_id)
    if not path.exists():
        return []
    entries = []
    try:
        with path.open("r", encoding=ENCODING) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit:
        entries = entries[-limit:]
    return entries


def clear(conv_id):
    if not conversations.is_valid_id(conv_id):
        return False
    try:
        _log_path(conv_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
# The Logs viewer shows raw provider request/response bodies — which is the
# point of it, and also why it's unusable past a few hundred entries without
# a way to find things. conv_search.py already searches conversation *text*;
# this searches the log JSON itself, which is a different haystack: tool
# arguments, error strings, model names, token counts, raw payload fields.
#
# Matching runs against the serialized JSON of each entry rather than a
# hand-picked set of fields. A log entry's shape varies by provider and by
# direction, so an allowlist of searchable keys would silently miss exactly
# the field someone is hunting for. Serializing once per entry and running a
# plain substring/regex over it is slower in theory and correct in practice.

SEARCH_MODES = ("words", "phrase", "regex")
SEARCH_SNIPPET_RADIUS = 90
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_PATTERN_CHARS = 500


def _compile_search(query, mode):
    """Return a list of compiled patterns, or (None, error).

    "words" -> every whitespace-separated term must appear (AND), which is
    what people actually expect from a search box. "phrase" -> the literal
    string. "regex" -> their pattern, compiled with a guard so a malformed
    one reports an error instead of raising up through the endpoint.
    """
    query = (query or "").strip()
    if not query:
        return None, "empty query"
    if len(query) > SEARCH_MAX_PATTERN_CHARS:
        return None, f"query too long (max {SEARCH_MAX_PATTERN_CHARS})"
    mode = mode if mode in SEARCH_MODES else "words"
    try:
        if mode == "regex":
            return [re.compile(query, re.IGNORECASE)], ""
        if mode == "phrase":
            return [re.compile(re.escape(query), re.IGNORECASE)], ""
        terms = [t for t in query.split() if t]
        if not terms:
            return None, "empty query"
        return [re.compile(re.escape(t), re.IGNORECASE) for t in terms], ""
    except re.error as exc:
        return None, f"bad regex: {exc}"


def _search_snippet(text, match):
    start = max(0, match.start() - SEARCH_SNIPPET_RADIUS)
    end = min(len(text), match.end() + SEARCH_SNIPPET_RADIUS)
    snippet = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def search(query, mode="words", limit=SEARCH_DEFAULT_LIMIT, conv_id=None,
           directions=None, origins=None, sources=None):
    """Search log entries. Returns {"ok", "results"|"error", ...}.

    conv_id    restrict to one conversation's log
    directions restrict to e.g. ["tool_call", "error"]
    origins    restrict to conversations from e.g. ["discord"] — this is
               what makes "show me every error the Discord bot hit" a
               single query rather than a manual hunt through the list.
    """
    patterns, err = _compile_search(query, mode)
    if patterns is None:
        return {"ok": False, "error": err, "results": []}

    wanted_dirs = set(directions or []) or None
    wanted_origins = set(origins or []) or None
    wanted_sources = set(sources or []) or None

    listing = list_logged_conversations()
    meta_by_id = {it["id"]: it for it in listing}
    conv_ids = [conv_id] if conv_id else [it["id"] for it in listing]

    results = []
    scanned = 0
    for cid in conv_ids:
        meta = meta_by_id.get(cid, {})
        if wanted_origins is not None and (meta.get("origin") or "") not in wanted_origins:
            continue
        for index, entry in enumerate(read_entries(cid)):
            scanned += 1
            direction = entry.get("direction") or ""
            if wanted_dirs is not None and direction not in wanted_dirs:
                continue
            if wanted_sources is not None and (entry.get("source") or "") not in wanted_sources:
                continue
            try:
                blob = json.dumps(entry, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                continue
            # "words" mode is an AND across terms, so every pattern has to
            # hit somewhere in this entry before it counts as a match.
            hits = [pattern.search(blob) for pattern in patterns]
            if not all(hits):
                continue
            first = hits[0]
            results.append({
                "conv_id": cid,
                "title": meta.get("title") or "(deleted conversation)",
                "origin": meta.get("origin") or "",
                "origin_detail": meta.get("origin_detail") or "",
                "entry_index": index,
                "ts": entry.get("ts"),
                "direction": direction,
                "source": entry.get("source") or "",
                "provider": entry.get("provider"),
                "round": entry.get("round"),
                "snippet": _search_snippet(blob, first),
            })
            if len(results) >= limit:
                return {"ok": True, "results": results, "truncated": True,
                        "scanned": scanned, "query": query, "mode": mode}
    return {"ok": True, "results": results, "truncated": False,
            "scanned": scanned, "query": query, "mode": mode}
