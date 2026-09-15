"""Full-text search across every stored conversation.

conversations.py already keeps each chat as a flat JSON file under
~/.jarvis/conversations/, and an index.json holding only title +
soft_context. That index is what `conv-list` searches today, which means
searching for a phrase you remember *saying* finds nothing unless it
happened to land in a generated title — the single most common way people
actually look for an old chat.

This module reads the transcripts themselves. It's a grep, not an embedding
index: no model call, no background job to keep warm, no extra state that
can go stale against the files it describes. On a few hundred conversations
that's a handful of milliseconds, and it can never disagree with what's on
disk — which matters more here than raw speed, because a search index that
silently lags is worse than no search at all.

WHAT IT SEARCHES
----------------
Every exchange's user text and reply text, plus (optionally) the tool
arguments and results recorded in that turn's `extras` — searching tool
traffic is off by default because "find where I talked about X" shouldn't
match a file path that merely contained X.

MATCH MODES
-----------
  words     (default) every whitespace-separated term must appear somewhere
            in the exchange, in any order, case-insensitively. This is what
            people expect from a search box and it tolerates half-remembered
            phrasing.
  phrase    the query as one contiguous substring.
  regex     a real regular expression, for when you know exactly what you're
            looking for. Compiled with a size cap and always case-insensitive
            unless the pattern says otherwise.

Results carry a snippet with the match in context plus the surrounding
turn, because a bare "conversation a1b2c3d4 matched" is useless for deciding
whether it's the one you wanted.
"""

import json
import re
from datetime import datetime

from . import conversations

# A pathological regex from a user typing into a search box shouldn't hang
# the CLI. Length cap plus a hard ceiling on how many exchanges any single
# search will scan.
MAX_PATTERN_CHARS = 500
MAX_SCAN_EXCHANGES = 20000
SNIPPET_RADIUS = 90
DEFAULT_LIMIT = 30
MAX_SNIPPETS_PER_CONV = 3

MODES = ("words", "phrase", "regex")


class SearchError(Exception):
    """Raised for a query the caller can fix (bad regex, empty query)."""


def _compile(query, mode):
    """Turn a query into a list of compiled patterns. `words` mode returns
    one pattern per term — all of which must match — which is what makes
    term order irrelevant without resorting to permutations."""
    query = (query or "").strip()
    if not query:
        raise SearchError("empty search query")
    if len(query) > MAX_PATTERN_CHARS:
        raise SearchError("search query is too long (max %d characters)" % MAX_PATTERN_CHARS)

    if mode == "regex":
        try:
            return [re.compile(query, re.IGNORECASE)]
        except re.error as e:
            raise SearchError("invalid regular expression: %s" % e)
    if mode == "phrase":
        return [re.compile(re.escape(query), re.IGNORECASE)]

    terms = [t for t in query.split() if t]
    if not terms:
        raise SearchError("empty search query")
    return [re.compile(re.escape(t), re.IGNORECASE) for t in terms]


def _exchange_text(exchange, include_tools=False):
    """Everything in one exchange worth matching against, as one string.

    Joined with newlines rather than spaces so a phrase search can't match
    across the seam between the user's message and the reply — "thanks
    Jarvis" shouldn't match a message ending "thanks" followed by a reply
    starting "Jarvis".
    """
    parts = [exchange.get("user") or "", exchange.get("jarvis") or ""]
    if include_tools:
        for extra in exchange.get("extras") or []:
            if not isinstance(extra, dict):
                continue
            try:
                parts.append(json.dumps(extra.get("data") or {}, default=str))
            except (TypeError, ValueError):
                continue
    return "\n".join(p for p in parts if p)


def _snippet(text, match):
    """A window of text around one match, with ellipses where it was cut and
    the match itself wrapped in «» so a caller can highlight it without
    re-running the regex."""
    start, end = match.span()
    left = max(0, start - SNIPPET_RADIUS)
    right = min(len(text), end + SNIPPET_RADIUS)
    fragment = text[left:right].replace("\n", " ").strip()
    rel_start, rel_end = start - left, end - left
    # Rebuild from the untrimmed slice so the marker offsets stay correct
    # even after the strip above shifted things.
    raw = text[left:right].replace("\n", " ")
    marked = raw[:rel_start] + "\u00ab" + raw[rel_start:rel_end] + "\u00bb" + raw[rel_end:]
    marked = marked.strip()
    prefix = "\u2026" if left > 0 else ""
    suffix = "\u2026" if right < len(text) else ""
    return prefix + (marked or fragment) + suffix


def _matches(text, patterns, mode):
    """(bool, first-match) for one blob. In `words` mode every pattern has to
    hit; the returned match is the first term's, since that's the one most
    likely to be what the user was picturing."""
    found = []
    for pattern in patterns:
        m = pattern.search(text)
        if not m:
            return False, None
        found.append(m)
    return True, found[0]


def search(query, mode="words", limit=DEFAULT_LIMIT, include_tools=False,
           conv_id=None, since=None, until=None):
    """Search every conversation. Returns a list of per-conversation hits,
    most-recently-updated first.

    `conv_id` restricts to one conversation (searching within a chat you
    already have open); `since`/`until` are ISO dates bounding the exchange
    timestamp.
    """
    mode = (mode or "words").strip().lower()
    if mode not in MODES:
        raise SearchError("mode must be one of: %s" % ", ".join(MODES))
    patterns = _compile(query, mode)

    since_dt = _parse_bound(since)
    until_dt = _parse_bound(until)

    if conv_id:
        if not conversations.is_valid_id(conv_id):
            raise SearchError("%r isn't a conversation id" % conv_id)
        targets = [{"id": conv_id}]
    else:
        targets = conversations.list_conversations() or []

    results, scanned = [], 0
    for entry in targets:
        cid = entry.get("id")
        if not cid:
            continue
        record = conversations.get_conversation(cid)
        if not record:
            continue
        hits = []
        for position, exchange in enumerate(record.get("exchanges") or []):
            scanned += 1
            if scanned > MAX_SCAN_EXCHANGES:
                break
            ts = _parse_bound(exchange.get("ts"))
            if since_dt and ts and ts < since_dt:
                continue
            if until_dt and ts and ts > until_dt:
                continue
            text = _exchange_text(exchange, include_tools=include_tools)
            if not text:
                continue
            ok, match = _matches(text, patterns, mode)
            if not ok:
                continue
            hits.append({
                "position": position,
                "ts": exchange.get("ts"),
                "user": _clip(exchange.get("user")),
                "jarvis": _clip(exchange.get("jarvis")),
                "snippet": _snippet(text, match) if match else "",
                "interrupted": exchange.get("interrupted"),
            })
        if hits:
            results.append({
                "id": cid,
                "title": record.get("title") or conversations.DEFAULT_TITLE,
                "updated_at": record.get("updated_at"),
                "match_count": len(hits),
                "matches": hits[:MAX_SNIPPETS_PER_CONV],
                "total_exchanges": len(record.get("exchanges") or []),
            })
        if scanned > MAX_SCAN_EXCHANGES:
            break

    results.sort(key=lambda r: (r.get("updated_at") or ""), reverse=True)
    return results[:limit]


def _clip(text, n=200):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n] + "\u2026"


def _parse_bound(value):
    if not value:
        return None
    raw = str(value).strip()
    for fmt in (None, "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            if fmt is None:
                # Conversation timestamps are timezone-aware UTC
                # (conversations._now uses timezone.utc). A bound the user
                # typed as a bare date is naive, so both sides are
                # normalized to naive here rather than half-comparing and
                # raising TypeError deep inside the scan loop.
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def summarize(results):
    """Flat counts for a header line, computed once here so the CLI, the web
    panel and the AI tool all report the same numbers."""
    return {
        "conversations": len(results),
        "matches": sum(r.get("match_count", 0) for r in results),
    }


def render_for_terminal(results, query):
    """Plain-text rendering for `jarvis conv-search`."""
    if not results:
        return "No conversations matched %r." % query
    lines = []
    counts = summarize(results)
    lines.append("%d match%s across %d conversation%s for %r:" % (
        counts["matches"], "" if counts["matches"] == 1 else "es",
        counts["conversations"], "" if counts["conversations"] == 1 else "s", query))
    for result in results:
        lines.append("")
        lines.append("  %s  %s  (%d match%s)" % (
            result["id"], result["title"], result["match_count"],
            "" if result["match_count"] == 1 else "es"))
        for hit in result["matches"]:
            when = (hit.get("ts") or "")[:16].replace("T", " ")
            lines.append("    [%s] %s" % (when, hit["snippet"]))
    return "\n".join(lines)
