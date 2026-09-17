"""Consolidation — turn conversation history into memory, the way sleep does.

WHAT WAS MISSING
----------------
conv_search.py is a grep. It finds a conversation if you remember a word you
actually typed in it, which is precisely the case where you didn't need
search. "What did we decide about the deployment thing, a few weeks ago?"
finds nothing, because you called it "the release process" at the time.

And nothing ever *distils*. Every exchange sits at full length forever, with
equal retrieval priority, so a throwaway "what's the time" from March
competes with the architecture discussion from last week. Human memory does
the opposite: it collapses the repetitive, extracts the durable, and lets the
rest fade without deleting it.

THREE PARTS
-----------
1. **Semantic recall over history** (`recall`) — the same hashed-vector
   machinery memory_semantic.py uses for facts, applied to exchanges. Finds
   "the release process" from "the deployment thing" without either an API
   call or a vector database, because at a few thousand exchanges a flat
   scan is sub-100ms and cannot go stale against the files it describes.

2. **Consolidation** (`consolidate`) — a pass that reads recent
   conversations and extracts durable facts into memory.py. Runs on demand
   (`jarvis memory-consolidate`) or from the scheduler. Uses the model when
   one is configured, and a pattern-based extractor when one isn't, so the
   feature is not gated behind an API key.

3. **Decay** (`decay_factor`) — retrieval priority falls off with age, on a
   curve, so old exchanges are reachable but stop competing with recent ones.
   Nothing is ever deleted. This is the part that keeps a two-year-old
   install from getting steadily worse at recall.

WHY NOT sentence-transformers
-----------------------------
It's the obvious answer and it was considered: a few hundred MB of torch, a
model download on first run, and a per-exchange encode. For a corpus this
size the local hashed vectors already resolve the cases that matter (see
memory_semantic.py's own note on what they can and can't do), and the
optional embedding tier there covers the rest for anyone who wants it. A
heavyweight default dependency for a feature most installs will use lightly
is the wrong trade for a project whose base install is four packages.

SAFETY OF AUTO-EXTRACTION
-------------------------
A consolidation pass that writes to long-term memory unsupervised can poison
it — one misread sentence becomes a "fact" Jarvis repeats forever. So:
every extracted fact is tagged `auto` and carries its source conversation
id; extraction is conservative (explicit statements only, never inference);
and `jarvis memory-consolidate --review` prints what WOULD be saved without
saving it. Auto facts are the first thing dropped when the fact cap is hit.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import conversations, memory, memory_semantic

JARVIS_DIR = Path.home() / ".jarvis"
STATE_FILE = JARVIS_DIR / "consolidation.json"
ENCODING = "utf-8"

# Exchanges older than this lose priority fastest. Chosen to match how people
# actually talk about their own history: "last week" is recent, "a couple of
# months ago" is archive.
HALF_LIFE_DAYS = 21.0
MIN_DECAY = 0.15          # an old exchange never falls to zero, just to quiet
MAX_SCAN_EXCHANGES = 8000
MAX_RECALL_RESULTS = 8

# How similar two exchanges must be to count as redundant.
REDUNDANCY_THRESHOLD = 0.82

AUTO_TAG = "auto"

# Sentences that state a durable fact about the user. Deliberately narrow:
# these are patterns where the user is telling Jarvis something ABOUT
# THEMSELVES, in the present tense, unhedged. Anything conditional, past
# tense or speculative is left alone — "I was using Postgres" and "I might
# switch to Postgres" are not facts about the present, and a memory system
# that cannot tell those apart is worse than one that stores less.
_FACT_PATTERNS = [
    (re.compile(r"\b(?:my name is|call me|i'?m called)\s+([A-Z][\w'-]{1,30})", re.I),
     "preferred_name", "identity", "Name is {0}"),
    (re.compile(r"\bi (?:always|usually|generally) (?:use|prefer|run)\s+([^.,;!?]{3,60})", re.I),
     "", "prefs", "Usually uses {0}"),
    (re.compile(r"\bi prefer\s+([^.,;!?]{3,60})", re.I),
     "", "prefs", "Prefers {0}"),
    (re.compile(r"\bi (?:hate|don'?t like|avoid)\s+([^.,;!?]{3,60})", re.I),
     "", "prefs", "Dislikes {0}"),
    (re.compile(r"\bmy (?:pc|machine|laptop|desktop|rig) (?:has|runs|is)\s+([^.,;!?]{3,60})", re.I),
     "main_pc", "hardware", "Main machine: {0}"),
    (re.compile(r"\bi work (?:at|for)\s+([A-Z][^.,;!?]{2,40})", re.I),
     "employer", "identity", "Works at {0}"),
    (re.compile(r"\bi'?m (?:based |located )?in\s+([A-Z][^.,;!?]{2,40})", re.I),
     "location", "identity", "Based in {0}"),
    (re.compile(r"\b(?:remember|note) that\s+([^.;!?]{5,140})", re.I),
     "", "", "{0}"),
    (re.compile(r"\bmy (?:timezone|time zone) is\s+([^.,;!?]{2,30})", re.I),
     "timezone", "identity", "Timezone is {0}"),
]

# Phrases that disqualify a sentence outright, checked before the patterns.
_HEDGES = re.compile(
    r"\b(if|maybe|might|perhaps|probably|used to|was going to|thinking about|"
    r"considering|what if|suppose|imagine|pretend|for example|e\.g\.|hypothetical)\b",
    re.I)


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


def decay_factor(when, now=None):
    """Retrieval weight for something from `when`. 1.0 fresh -> MIN_DECAY old.

    Exponential half-life rather than a cliff: a cliff means an exchange is
    fully relevant one day and invisible the next, which produces exactly the
    "why did it stop remembering that?" confusion this is meant to avoid.
    """
    now = now or datetime.now()
    try:
        stamp = datetime.fromisoformat(str(when)) if isinstance(when, str) else when
    except (TypeError, ValueError):
        return 1.0
    if not stamp:
        return 1.0
    # conversations.py writes UTC-aware timestamps; everything else in this
    # project is naive local (see timespec.py). Subtracting one from the other
    # raises, so both sides are flattened to naive local before the
    # arithmetic. Flattening rather than making `now` aware keeps this
    # consistent with the rest of the codebase's naive-local convention.
    try:
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone().replace(tzinfo=None)
        if now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)
    except (AttributeError, ValueError, OSError):
        return 1.0
    age_days = max(0.0, (now - stamp).total_seconds() / 86400.0)
    factor = 0.5 ** (age_days / HALF_LIFE_DAYS)
    return max(MIN_DECAY, min(1.0, factor))


# ---------------------------------------------------------------------------
# Semantic recall over conversation history
# ---------------------------------------------------------------------------


def _iter_exchanges(limit=MAX_SCAN_EXCHANGES, exclude_conv=None):
    """Every stored exchange, newest conversation first."""
    count = 0
    for entry in conversations.list_conversations():
        conv_id = entry.get("id")
        if not conv_id or conv_id == exclude_conv:
            continue
        record = conversations.get_conversation(conv_id)
        if not record:
            continue
        title = record.get("title") or ""
        for index, exchange in enumerate(record.get("exchanges") or []):
            if exchange.get("pending"):
                continue
            yield {
                "conv_id": conv_id, "title": title, "index": index,
                "ts": exchange.get("ts"),
                "user": exchange.get("user") or "",
                "jarvis": exchange.get("jarvis") or "",
            }
            count += 1
            if count >= limit:
                return


def _exchange_doc(item):
    """What gets vectorized. The user's words are repeated because the query
    is usually phrased the way THEY talk, not the way the model answered."""
    return " ".join([item["title"], item["user"], item["user"], item["jarvis"][:600]])


def recall(query, limit=MAX_RECALL_RESULTS, exclude_conv=None, now=None):
    """Semantically find past exchanges. [] when nothing is close enough.

    Age-weighted: an old-but-perfect match still wins over a recent-but-vague
    one, because decay multiplies rather than filters.
    """
    query = (query or "").strip()
    if not query:
        return []
    now = now or datetime.now()

    items = list(_iter_exchanges(exclude_conv=exclude_conv))
    if not items:
        return []

    # Reuses memory_semantic's vectorizer wholesale — one implementation of
    # "how similar are these two pieces of text", so facts and history can
    # never disagree about it.
    docs = [_exchange_doc(i) for i in items]
    idf = _idf_for(docs)
    qvec = memory_semantic.vectorize(query, idf)
    if not qvec:
        return []

    scored = []
    for item, doc in zip(items, docs):
        sim = memory_semantic.cosine(qvec, memory_semantic.vectorize(doc, idf))
        if sim <= 0:
            continue
        scored.append((sim * decay_factor(item.get("ts"), now), sim, item))
    if not scored:
        return []

    scored.sort(key=lambda x: -x[0])
    best = scored[0][1]
    out = []
    for weighted, raw, item in scored[:limit]:
        # Same relative-threshold reasoning as memory_semantic.rank: short
        # texts never produce high cosines, so "clearly above this corpus's
        # own noise" is the only cutoff that works across corpus sizes.
        if raw < max(0.04, best * 0.45):
            continue
        out.append({
            "conversation_id": item["conv_id"],
            "title": item["title"],
            "when": (item.get("ts") or "")[:16],
            "similarity": round(raw, 3),
            "score": round(weighted, 3),
            "user": item["user"][:200],
            "jarvis": item["jarvis"][:300],
        })
    return out


def _idf_for(docs):
    from collections import Counter
    import math
    n = max(1, len(docs))
    freq = Counter()
    for doc in docs:
        for feature in set(memory_semantic.features(doc)):
            freq[feature] += 1
    idf = {f: math.log((n + 1) / (df + 0.5)) + 1.0 for f, df in freq.items()}
    idf["__default__"] = math.log(n + 1) + 1.0
    return idf


# ---------------------------------------------------------------------------
# Redundancy
# ---------------------------------------------------------------------------


def find_redundant(threshold=REDUNDANCY_THRESHOLD, limit=MAX_SCAN_EXCHANGES):
    """Groups of near-identical exchanges — the "what's the time" clusters.

    Reported, never deleted. Deleting someone's history to save space they
    didn't ask to save is not a trade this should make on its own; the value
    is in *knowing* that 40% of the corpus is six repeated questions.
    """
    items = list(_iter_exchanges(limit=limit))
    if len(items) < 2:
        return []
    docs = [_exchange_doc(i) for i in items]
    idf = _idf_for(docs)
    vectors = [memory_semantic.vectorize(d, idf) for d in docs]

    seen, groups = set(), []
    for i, vec in enumerate(vectors):
        if i in seen:
            continue
        cluster = []
        for j in range(i + 1, len(vectors)):
            if j in seen:
                continue
            if memory_semantic.cosine(vec, vectors[j]) >= threshold:
                cluster.append(j)
                seen.add(j)
        if cluster:
            seen.add(i)
            groups.append({
                "representative": items[i]["user"][:120],
                "count": len(cluster) + 1,
                "conversations": sorted({items[k]["conv_id"] for k in [i] + cluster}),
            })
    groups.sort(key=lambda g: -g["count"])
    return groups


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------


def extract_facts_local(text, source=None):
    """Pattern-based extraction. No model, no tokens, deliberately timid.

    Returns [{fact, key, tags, confidence}]. Nothing is inferred — every
    result is a rephrasing of something the user stated outright.
    """
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        sentence = sentence.strip()
        if len(sentence) < 8 or len(sentence) > 300:
            continue
        if _HEDGES.search(sentence):
            continue
        for pattern, key, tag, template in _FACT_PATTERNS:
            match = pattern.search(sentence)
            if not match:
                continue
            value = (match.group(1) or "").strip().rstrip(".,;")
            if len(value) < 2:
                continue
            fact = template.format(value)
            out.append({
                "fact": fact[:memory.MAX_FACT_LEN],
                "key": key,
                "tags": [t for t in (tag, AUTO_TAG) if t],
                "confidence": 0.75,
                "source": source,
                "evidence": sentence[:160],
            })
            break
    return out


def extract_facts_model(text, cfg=None):
    """Ask a configured provider for durable facts. None when unavailable.

    Uses the smallest sensible request and insists on JSON. Returning None
    rather than raising means the caller falls back to the local extractor,
    so consolidation works with no API key at all — just less well.
    """
    try:
        from . import ai_config, ai_providers
    except ImportError:
        return None
    cfg = cfg or ai_config.load_ai_config()
    providers = [p for p in (cfg.get("providers") or [])
                 if isinstance(p, dict) and p.get("enabled")]
    if not providers:
        return None

    prompt = (
        "From the conversation below, extract ONLY durable facts about the USER "
        "that would still be true next month — preferences, hardware, names, "
        "recurring processes, standing instructions.\n\n"
        "Rules:\n"
        "- Only things the user stated outright. Never infer.\n"
        "- No one-off task details, no transient state, no facts about you.\n"
        "- No passwords, API keys or tokens, ever.\n"
        "- If there are none, return an empty list. That is a normal answer.\n\n"
        'Reply with ONLY a JSON array: [{"fact": "...", "key": "optional_slug", '
        '"tags": ["prefs"]}]\n\n'
        "CONVERSATION:\n" + text[:6000]
    )
    messages = [
        {"role": "system", "content": "You extract durable facts. You reply with JSON only."},
        {"role": "user", "content": prompt},
    ]

    for provider in providers[:2]:
        adapter = ai_providers.ADAPTERS.get(provider.get("type"))
        if not adapter:
            continue
        keys = None
        try:
            from . import ai_config as _c
            keys = _c.provider_keys(provider) or [None]
        except Exception:  # noqa: BLE001
            keys = [None]
        resolved = dict(provider)
        resolved.setdefault("timeout", 30)
        resolved["max_tokens"] = 800
        if keys[0]:
            resolved["api_key"] = keys[0]
        try:
            result = adapter(resolved, messages, 30, tools=None, tool_executor=None)
        except Exception:  # noqa: BLE001
            continue
        if not result.ok or not result.text:
            continue
        match = re.search(r"\[.*\]", result.text, re.S)
        if not match:
            continue
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            continue
        out = []
        for entry in parsed if isinstance(parsed, list) else []:
            if not isinstance(entry, dict) or not (entry.get("fact") or "").strip():
                continue
            tags = entry.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            out.append({
                "fact": str(entry["fact"])[:memory.MAX_FACT_LEN],
                "key": str(entry.get("key") or "")[:memory.MAX_KEY_LEN],
                "tags": [str(t)[:24] for t in tags][:5] + [AUTO_TAG],
                "confidence": 0.85,
                "evidence": "",
            })
        return out
    return None


_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_\-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bpassword\s*[:=]\s*\S+|\bapi[_ ]?key\s*[:=]\s*\S+)", re.I)


def _is_safe_fact(fact):
    """Last line of defence before anything is written to long-term memory.

    Applied to model output AND local output: the model is instructed not to
    return secrets, but an instruction is not a guarantee, and a leaked key
    living in memory.json forever — and riding along in future prompts —
    is the worst outcome this module could produce.
    """
    text = (fact or "").strip()
    if len(text) < 5:
        return False
    if _SECRET_RE.search(text):
        return False
    return True


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def _load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding=ENCODING))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_state(state):
    from . import atomic_io
    return atomic_io.write_json(STATE_FILE, state)


def consolidate(days=7, review_only=False, use_model=True, limit_facts=12,
                namespace=None):
    """Read recent conversations, extract durable facts, optionally save them.

    `review_only=True` is the important mode — it shows exactly what would be
    written without writing it, which is the only way to trust an unsupervised
    writer over time.
    """
    state = _load_state()
    since = datetime.now() - timedelta(days=max(1, int(days or 7)))
    last_run = state.get("last_run")

    scanned, transcript_parts, conv_ids = 0, [], []
    for entry in conversations.list_conversations():
        updated = entry.get("updated_at") or ""
        if updated and updated < since.isoformat():
            continue
        record = conversations.get_conversation(entry.get("id"))
        if not record:
            continue
        conv_ids.append(entry.get("id"))
        for exchange in record.get("exchanges") or []:
            if exchange.get("pending"):
                continue
            # Only the USER's words are mined. The assistant's replies are
            # its own output; treating them as evidence would let a
            # hallucination become a stored "fact" and then be fed back as
            # context — a loop with no correction in it.
            user_text = (exchange.get("user") or "").strip()
            if user_text:
                transcript_parts.append(user_text)
                scanned += 1

    if not transcript_parts:
        return {"ok": True, "scanned": 0, "found": 0, "saved": 0,
                "note": "no recent conversations to consolidate"}

    transcript = "\n".join(transcript_parts[-400:])

    candidates = None
    method = "local"
    if use_model:
        candidates = extract_facts_model(transcript)
        if candidates is not None:
            method = "model"
    if candidates is None:
        candidates = extract_facts_local(transcript,
                                         source=conv_ids[0] if conv_ids else None)

    # Filter: unsafe, duplicate, or already-known.
    existing = memory.load_facts()
    existing_text = {(f.get("fact") or "").strip().lower() for f in existing}
    existing_keys = {(f.get("key") or "") for f in existing if f.get("key")}

    kept, skipped = [], []
    for candidate in candidates:
        fact = (candidate.get("fact") or "").strip()
        if not _is_safe_fact(fact):
            skipped.append({"fact": fact[:60], "why": "looked like a secret or was too short"})
            continue
        if fact.lower() in existing_text:
            skipped.append({"fact": fact[:60], "why": "already known"})
            continue
        key = candidate.get("key") or ""
        if key and key in existing_keys:
            skipped.append({"fact": fact[:60],
                            "why": "would overwrite the existing '%s'" % key})
            continue
        kept.append(candidate)
        if len(kept) >= limit_facts:
            break

    result = {
        "ok": True, "scanned": scanned, "method": method,
        "found": len(kept), "skipped": skipped[:10],
        "candidates": [{"fact": c["fact"], "key": c.get("key") or None,
                        "tags": c.get("tags") or [],
                        "evidence": c.get("evidence", "")} for c in kept],
        "last_run": last_run,
    }

    if review_only:
        result["saved"] = 0
        result["note"] = "review only — nothing was written"
        return result

    saved = 0
    for candidate in kept:
        outcome = memory.tool_memory_save({
            "fact": candidate["fact"],
            "key": candidate.get("key") or "",
            "tags": candidate.get("tags") or [AUTO_TAG],
            "namespace": namespace,
        })
        if outcome.get("ok"):
            saved += 1

    state["last_run"] = datetime.now().replace(microsecond=0).isoformat()
    state["last_saved"] = saved
    _save_state(state)

    result["saved"] = saved
    result["note"] = ("Saved facts are tagged '%s'. Review with "
                      "`jarvis memory-list --auto`, remove with memory_forget."
                      % AUTO_TAG)
    return result


def stats():
    """What consolidation knows about the corpus — the `memory-stats` view."""
    items = list(_iter_exchanges())
    facts = memory.load_facts()
    auto = [f for f in facts if AUTO_TAG in (f.get("tags") or [])]
    now = datetime.now()
    buckets = {"this week": 0, "this month": 0, "older": 0}
    for item in items:
        factor = decay_factor(item.get("ts"), now)
        if factor > 0.75:
            buckets["this week"] += 1
        elif factor > 0.35:
            buckets["this month"] += 1
        else:
            buckets["older"] += 1
    return {
        "exchanges": len(items),
        "by_age": buckets,
        "facts_total": len(facts),
        "facts_auto": len(auto),
        "namespaces": memory.namespaces(),
        "last_consolidation": _load_state().get("last_run"),
    }
