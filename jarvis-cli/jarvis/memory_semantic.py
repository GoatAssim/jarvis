"""Semantic search over long-term memory — the fuzzy half of memory_search.

WHAT WAS WRONG
--------------
memory_search was a substring match, and prompt_context's relevance scoring
was exact-token overlap. Both work beautifully right up until the words
don't line up:

    saved:  "Prefers dark mode in every app"
    asked:  "do I like light themes?"          -> no shared token, no match

    saved:  "Main PC is a Ryzen 7800X3D with an RTX 4080"
    asked:  "what graphics card do I have"     -> only helped because someone
                                                  hand-wrote gpu->rtx into
                                                  memory._EXPAND

That hand-written synonym table is the tell. It works, it will never be
finished, and every fact the user saves in their own words is one the table
doesn't know about. As the facts file grows, the failure mode isn't "wrong
answer" — it's Jarvis confidently saying it doesn't know something it wrote
down last week.

TWO TIERS, BOTH OPTIONAL-FREE
-----------------------------
**Tier 1 — local, always available, zero dependencies.** A hashed
bag-of-features vector per fact: word tokens (lightly stemmed), word
bigrams, and character 4-grams, IDF-weighted against the fact corpus and
L2-normalized, compared by cosine. Character n-grams are what make this
genuinely fuzzy rather than a fancier exact match — "graphics"/"graphic",
"playnite"/"plaknite", "schedule"/"scheduling" all overlap heavily at the
4-gram level with no dictionary and no stemmer.

This is not as good as real embeddings. It IS good enough to catch
morphology, typos, compounds and partial phrases, it runs in under a
millisecond on a few hundred facts, and — the part that matters most for
this codebase — it adds no dependency to a project whose base install is
four packages on purpose (see timespec.py's own "no external dependency"
note for the same reasoning).

**Tier 2 — real embeddings, when a provider is configured.** If an
embedding-capable provider exists in ai_config.json, `reindex()` embeds
every fact once and caches the vectors on disk keyed by a content hash, so
only new/edited facts are ever re-embedded. Query embedding costs one small
request per ask that uses it — which is why it is opt-in
(`memory.semantic.embeddings: true`) rather than on by default.

WHY BLEND INSTEAD OF REPLACE
----------------------------
Exact matches must still win. If a fact is keyed `preferred_name` and the
user asks about their preferred name, no similarity score should be able to
rank a vaguely-related fact above it. So the final ranking is the existing
lexical score plus a *bounded* semantic contribution — semantic search adds
recall without ever taking precision away. A fact that the old code would
have surfaced is still surfaced, in the same order, and the change is purely
that facts it would have missed now appear below them.

STALENESS
---------
The local index is derived, never authoritative: it's rebuilt from
memory.json whenever the fact count or content hash changes, and a corrupt
or missing index file just means "rebuild it now". This is the same
degrades-to-recompute contract as discovery_cache.py, and for the same
reason — an index that can silently disagree with the file it describes is
worse than no index.
"""

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
INDEX_FILE = JARVIS_DIR / "memory_index.json"
ENCODING = "utf-8"

# Hashed feature space. 4096 is plenty for a few hundred short facts (the
# MAX_FACTS cap is 80) and keeps the on-disk index small; collisions at this
# ratio are rare and, when they happen, cost a little precision rather than
# correctness.
DIMENSIONS = 4096
NGRAM_SIZE = 4

# How much semantic similarity can contribute on top of the lexical score.
# Deliberately below the lexical score a single key hit earns (8 + 3*n in
# memory._score_fact), so a keyed exact match always outranks a purely
# semantic one — see "WHY BLEND INSTEAD OF REPLACE" above.
SEMANTIC_WEIGHT = 6.0
# --- Thresholding ------------------------------------------------------------
#
# An ABSOLUTE cosine cutoff turns out to be the wrong instrument here, and the
# measurement says so plainly. For "do I like light themes?" against a
# three-fact corpus:
#
#     0.120  Prefers dark mode in every app          <- the right answer
#     0.018  Deploys go out Thursdays...
#     0.015  Main PC has an RTX 4080...
#
# The ranking is correct and the separation is 8x, but every score is low,
# because these vectors are dominated by character n-grams spread across
# thousands of dimensions — short texts simply never produce high cosines in
# this space. A cutoff set high enough to reject the 0.018 noise on THIS query
# would reject the 0.120 signal on a slightly longer one.
#
# So the threshold is relative: a fact qualifies if it clears a low absolute
# floor AND stands meaningfully clear of the corpus's own noise level for this
# query. That adapts to a two-fact corpus and an eighty-fact one without
# retuning, which an absolute number cannot.
MIN_SIMILARITY = 0.045          # absolute floor — below this it is noise
RELATIVE_FACTOR = 2.5           # must beat the median match by this much
TOP_SCORE_FRACTION = 0.35       # ...or be within this fraction of the best

_WORD_RE = re.compile(r"[a-z0-9]+")

# Suffix stripping, longest-first. Not a real stemmer — Porter would be
# ~120 lines to marginally improve on this, and the character n-grams
# already cover most of what a stemmer would.
_SUFFIXES = ("ational", "iveness", "fulness", "ousness", "ization", "ization",
             "ations", "ingly", "edly", "ment", "ness", "tion", "sion",
             "ing", "ers", "est", "ies", "ied", "ive", "ful", "ous",
             "ed", "er", "ly", "s")

_STOP = frozenset({
    "a", "an", "the", "to", "of", "and", "or", "is", "it", "in", "on", "for",
    "that", "this", "you", "your", "do", "does", "did", "can", "could",
    "would", "please", "just", "want", "with", "from", "are", "was", "be",
    "am", "we", "they", "as", "at", "by", "if", "not", "my", "i", "me",
    "what", "when", "where", "which", "who", "why", "how", "have", "has",
})


# Domain synonym expansion. memory.py already maintains this table for its
# lexical scorer; reusing it rather than duplicating it means adding a synonym
# in one place improves both layers, and they can't drift apart.
#
# This is the local tier's answer to true synonymy, which character n-grams
# genuinely cannot reach — "gpu" and "RTX 4080" share no letters. It is a
# curated list and will never be complete; that is what the embedding tier is
# for. What it does buy is the handful of pairs this project actually sees.
def _expansions():
    try:
        from .memory import _EXPAND
        return _EXPAND
    except Exception:  # noqa: BLE001 — never let an import break search
        return {}


def _stem(word):
    if len(word) <= 4:
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _bucket(feature):
    """Stable hash -> dimension. md5 rather than Python's hash() because
    hash() is randomized per process (PYTHONHASHSEED), which would make a
    cached index meaningless across the fresh-process-per-call model this
    whole codebase runs on (see history.py)."""
    digest = hashlib.md5(feature.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % DIMENSIONS


def features(text):
    """Every feature of one string, with per-feature weights.

    Three families, weighted by how much signal each carries:
      words (1.0)      the primary signal
      bigrams (0.6)    "dark mode" means more than "dark" + "mode"
      4-grams (0.35)   the fuzzy layer — typos, morphology, compounds
    """
    text = (text or "").lower()
    raw_words = _WORD_RE.findall(text)
    words = [_stem(w) for w in raw_words if w not in _STOP and len(w) > 1]

    out = Counter()
    expand = _expansions()
    for word in words:
        out["w:" + word] += 1.0
    # Expansions are added at reduced weight — a synonym is evidence, not the
    # same word, and weighting it equally would let "music" match a Spotify
    # fact as strongly as the word "spotify" itself.
    for word in set(raw_words):
        for synonym in expand.get(word, ()):
            out["w:" + _stem(synonym)] += 0.55
    for a, b in zip(words, words[1:]):
        out["b:%s_%s" % (a, b)] += 0.6

    # Character n-grams over the *unstopped* text, so short but meaningful
    # tokens ("rtx", "4080") still contribute even though they'd survive the
    # word pass anyway — and so phrase shape is partly preserved.
    joined = " ".join(raw_words)
    if len(joined) >= NGRAM_SIZE:
        for i in range(len(joined) - NGRAM_SIZE + 1):
            gram = joined[i:i + NGRAM_SIZE]
            if gram.strip():
                out["c:" + gram] += 0.35
    return out


def _fact_text(fact):
    """Everything about a fact that carries meaning, with the key and tags
    repeated — they're the user's own labels, so they deserve more weight
    than a word buried in the middle of a sentence."""
    key = (fact.get("key") or "").replace("_", " ")
    tags = " ".join(str(t) for t in (fact.get("tags") or []))
    body = fact.get("fact") or ""
    return " ".join([key, key, tags, tags, body])


def vectorize(text, idf=None):
    """Hashed, IDF-weighted, L2-normalized sparse vector as {dim: weight}."""
    vec = {}
    for feature, weight in features(text).items():
        if idf is not None:
            weight *= idf.get(feature, idf.get("__default__", 1.0))
        dim = _bucket(feature)
        vec[dim] = vec.get(dim, 0.0) + weight
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm <= 0:
        return {}
    return {d: v / norm for d, v in vec.items()}


def cosine(a, b):
    """Dot product of two normalized sparse vectors — iterate the smaller."""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(dim, 0.0) for dim, weight in a.items())


def _compute_idf(facts):
    """Inverse document frequency across the fact corpus.

    With 80 facts this matters more than it sounds: without it, the word
    "prefers" (which appears in half of everyone's memory file) would count
    as much as "4080".
    """
    n = max(1, len(facts))
    doc_freq = Counter()
    for fact in facts:
        for feature in set(features(_fact_text(fact))):
            doc_freq[feature] += 1
    idf = {f: math.log((n + 1) / (df + 0.5)) + 1.0 for f, df in doc_freq.items()}
    idf["__default__"] = math.log(n + 1) + 1.0
    return idf


def _corpus_hash(facts):
    """Changes whenever any fact's searchable content changes, so a stale
    index is detected without timestamping every write."""
    h = hashlib.md5()
    for fact in facts:
        h.update((fact.get("id") or "").encode("utf-8"))
        h.update(_fact_text(fact).encode("utf-8"))
    h.update(str(DIMENSIONS).encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Index persistence
# ---------------------------------------------------------------------------


def _load_index():
    try:
        data = json.loads(INDEX_FILE.read_text(encoding=ENCODING))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("version") != 2:
        return None
    return data


def _save_index(data):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding=ENCODING)
        tmp.replace(INDEX_FILE)
        return True
    except OSError:
        # A derived index that can't be written just means recompute next
        # time. Never worth failing a memory_save over.
        return False


def build_index(facts, persist=True):
    """(Re)build the local index from scratch. Returns the index dict."""
    idf = _compute_idf(facts)
    vectors = {}
    for fact in facts:
        fid = fact.get("id")
        if not fid:
            continue
        vec = vectorize(_fact_text(fact), idf)
        # Stored as parallel arrays rather than an object: JSON object keys
        # are strings, so {dim: weight} would round-trip every dimension
        # through str()/int() on load, for hundreds of entries per fact.
        vectors[fid] = [list(vec.keys()), [round(v, 5) for v in vec.values()]]
    index = {
        "version": 2,
        "hash": _corpus_hash(facts),
        "idf": {k: round(v, 5) for k, v in idf.items()},
        "vectors": vectors,
    }
    if persist:
        _save_index(index)
    return index


def get_index(facts, rebuild_if_stale=True):
    """The current index, rebuilding it when memory.json has moved on."""
    index = _load_index()
    if index is not None and index.get("hash") == _corpus_hash(facts):
        return index
    if not rebuild_if_stale:
        return None
    return build_index(facts)


def _vector_from_index(index, fact_id):
    stored = (index.get("vectors") or {}).get(fact_id)
    if not stored or len(stored) != 2:
        return {}
    dims, weights = stored
    return dict(zip(dims, weights))


# ---------------------------------------------------------------------------
# Embeddings (tier 2)
# ---------------------------------------------------------------------------

# Provider name -> (endpoint template, model, request/response shape). Only
# hosts whose embedding endpoint is a plain HTTP POST are here; adding one is
# a dict entry, not a new code path.
_EMBED_PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1/embeddings",
        "model": "text-embedding-3-small",
        "style": "openai",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/"
               "text-embedding-004:batchEmbedContents",
        "model": "models/text-embedding-004",
        "style": "gemini",
    },
    "mistral": {
        "url": "https://api.mistral.ai/v1/embeddings",
        "model": "mistral-embed",
        "style": "openai",
    },
    "voyage": {
        "url": "https://api.voyageai.com/v1/embeddings",
        "model": "voyage-3-lite",
        "style": "openai",
    },
}


def embedding_provider(cfg=None):
    """The first configured provider that can embed, or None.

    Anthropic is deliberately absent: it has no embeddings endpoint, and
    silently falling back to another provider's key would be a surprising
    thing to do with someone's API credit.
    """
    from . import ai_config

    cfg = cfg or ai_config.load_ai_config()
    for provider in cfg.get("providers") or []:
        if not isinstance(provider, dict) or not provider.get("enabled"):
            continue
        name = (provider.get("name") or "").strip().lower()
        spec = _EMBED_PROVIDERS.get(name)
        if not spec:
            continue
        keys = ai_config.provider_keys(provider)
        if not keys:
            continue
        return {"name": name, "key": keys[0], **spec}
    return None


def embed_texts(texts, provider=None, timeout=20):
    """Embed a batch. Returns a list of vectors (or None on any failure).

    Returning None rather than raising is deliberate: every caller's correct
    response to "embeddings unavailable" is to fall back to the local index,
    which always works.
    """
    provider = provider or embedding_provider()
    if not provider or not texts:
        return None
    try:
        import requests
    except ImportError:
        return None

    try:
        if provider["style"] == "openai":
            resp = requests.post(
                provider["url"],
                headers={"Authorization": "Bearer %s" % provider["key"],
                         "Content-Type": "application/json"},
                json={"model": provider["model"], "input": list(texts)},
                timeout=timeout,
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            return [item.get("embedding") for item in (data.get("data") or [])]

        if provider["style"] == "gemini":
            resp = requests.post(
                provider["url"],
                params={"key": provider["key"]},
                json={"requests": [
                    {"model": provider["model"],
                     "content": {"parts": [{"text": t}]}}
                    for t in texts
                ]},
                timeout=timeout,
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            return [e.get("values") for e in (data.get("embeddings") or [])]
    except Exception:  # noqa: BLE001 — see docstring
        return None
    return None


def _dense_cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def reindex_embeddings(facts, provider=None):
    """Embed every fact that isn't already cached at its current content.

    Keyed on a hash of the fact's text, so editing a fact re-embeds exactly
    that one and nothing else — the reason this is cheap enough to run from
    a CLI command rather than a background job.
    """
    provider = provider or embedding_provider()
    if not provider:
        return {"ok": False, "error": "no embedding-capable provider is configured",
                "hint": "Add an OpenAI, Gemini, Mistral or Voyage key to "
                        "ai_config.json — or just use the built-in local index, "
                        "which needs nothing."}

    index = _load_index() or {}
    cached = index.get("embeddings") or {}
    pending, pending_ids = [], []
    for fact in facts:
        fid = fact.get("id")
        if not fid:
            continue
        text = _fact_text(fact)
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
        entry = cached.get(fid)
        if isinstance(entry, dict) and entry.get("h") == digest:
            continue
        pending.append(text)
        pending_ids.append((fid, digest))

    if not pending:
        return {"ok": True, "embedded": 0, "cached": len(cached),
                "provider": provider["name"], "note": "already up to date"}

    vectors = embed_texts(pending, provider)
    if not vectors or len(vectors) != len(pending_ids):
        return {"ok": False, "error": "the embedding request failed",
                "provider": provider["name"],
                "hint": "Local semantic search still works — this only affects "
                        "the higher-quality embedding tier."}

    for (fid, digest), vector in zip(pending_ids, vectors):
        if vector:
            cached[fid] = {"h": digest, "v": [round(float(x), 5) for x in vector]}

    # Drop embeddings for facts that no longer exist, so the file can't grow
    # forever as facts are forgotten and re-saved.
    live = {f.get("id") for f in facts}
    cached = {k: v for k, v in cached.items() if k in live}

    index = get_index(facts) or {}
    index["embeddings"] = cached
    index["embed_provider"] = provider["name"]
    _save_index(index)
    return {"ok": True, "embedded": len(pending_ids), "cached": len(cached),
            "provider": provider["name"]}


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank(query, facts, limit=None, use_embeddings=False, min_similarity=None):
    """Score every fact against the query semantically.

    Returns [(similarity, fact)], highest first, filtered to
    min_similarity. Purely additive to memory.py's lexical scoring — see the
    module docstring on why this never *removes* a lexical match.
    """
    query = (query or "").strip()
    if not query or not facts:
        return []
    threshold = MIN_SIMILARITY if min_similarity is None else min_similarity

    index = get_index(facts)
    scored = []

    dense = None
    if use_embeddings and (index.get("embeddings") if index else None):
        query_vec = embed_texts([query])
        if query_vec and query_vec[0]:
            dense = query_vec[0]

    if dense is not None:
        cached = index.get("embeddings") or {}
        for fact in facts:
            entry = cached.get(fact.get("id"))
            if not isinstance(entry, dict):
                continue
            sim = _dense_cosine(dense, entry.get("v") or [])
            if sim >= threshold:
                scored.append((sim, fact))
        if scored:
            scored.sort(key=lambda x: -x[0])
            return scored[:limit] if limit else scored
        # No dense hit: fall through to the local index rather than
        # returning nothing. An embedding miss should degrade to the
        # always-available tier, not to silence.

    idf = index.get("idf") if index else None
    qvec = vectorize(query, idf)
    if not qvec:
        return []

    raw = []
    for fact in facts:
        fid = fact.get("id")
        fvec = _vector_from_index(index, fid) if index else vectorize(_fact_text(fact), idf)
        sim = cosine(qvec, fvec)
        if sim > 0:
            raw.append((sim, fact))
    if not raw:
        return []

    raw.sort(key=lambda x: -x[0])
    scored = [(sim, fact) for sim, fact in raw
              if _qualifies(sim, raw, threshold)]
    return scored[:limit] if limit else scored


def _qualifies(sim, ranked, floor):
    """Is this score real signal, or this corpus's background noise?

    Two ways to qualify, because one test alone fails in a predictable case
    each: the median test breaks down when almost everything matches a little
    (a corpus of near-duplicates), and the top-fraction test breaks down when
    the single best match is itself weak. Passing either is enough.
    """
    if sim < floor:
        return False
    best = ranked[0][0]
    if best <= 0:
        return False
    scores = sorted(s for s, _f in ranked)
    median = scores[len(scores) // 2]
    if median > 0 and sim >= median * RELATIVE_FACTOR:
        return True
    return sim >= best * (1.0 - TOP_SCORE_FRACTION)


def boost_map(query, facts, use_embeddings=False):
    """{fact_id: extra_score} for memory.prompt_context to add to its own.

    Bounded by SEMANTIC_WEIGHT so semantic relevance can lift a fact into
    consideration but never outrank an exact key/tag hit.
    """
    out = {}
    for sim, fact in rank(query, facts, use_embeddings=use_embeddings):
        fid = fact.get("id")
        if fid:
            out[fid] = SEMANTIC_WEIGHT * sim
    return out


def explain(query, facts, limit=5, use_embeddings=False):
    """Human-readable ranking, for `jarvis memory-search --semantic` and for
    checking by hand whether a disappointing result is a scoring problem or
    a genuinely absent fact."""
    rows = []
    for sim, fact in rank(query, facts, limit=limit, use_embeddings=use_embeddings):
        rows.append({
            "similarity": round(sim, 3),
            "id": fact.get("id"),
            "key": fact.get("key") or None,
            "fact": fact.get("fact"),
            "tags": fact.get("tags") or [],
            "namespace": fact.get("namespace") or "default",
        })
    return rows
