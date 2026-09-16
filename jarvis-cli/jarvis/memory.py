"""Durable long-term memory for Jarvis (~/.jarvis/memory.json).

Conversation history is a short rolling chat log. This file is the permanent
notebook: names, preferences, hardware, 'remember that…' facts.

Facts are NOT dumped into every prompt. Each ask retrieves only facts that
look relevant to the user message (and recent user turns), except identity
facts (name, timezone, etc.), which ride along in full every time. Explicit
'what do you remember' style questions load as many as the budget allows.
Anything relevant that didn't fit the budget is listed by label in a
trailing index so the model can still memory_search it by name instead of
guessing it doesn't exist.
"""

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "memory.json"
ENCODING = "utf-8"

MAX_FACTS = 80
MAX_FACT_LEN = 280
MAX_KEY_LEN = 48
PROMPT_FULL_BUDGET = 1200
PROMPT_COMPACT_BUDGET = 450
MAX_PROMPT_FACTS = 8
# Ported from Mark LIII's memory_manager.PROMPT_MAX_PER_CATEGORY: caps how many
# facts sharing a primary tag may occupy the core block, so one chatty tag
# (e.g. a dozen "games" facts) can't crowd out everything else that matched.
PROMPT_MAX_PER_TAG = 3
# Budget for the trailing "also remembered" index of facts that matched but
# didn't fit. Same idea as Mark LIII's PROMPT_INDEX_CHARS: the model can't
# decide to memory_search something it doesn't know exists, so instead of a
# bare "(N other facts not shown)" count we hand back the actual labels.
PROMPT_INDEX_CHARS = 300

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_WORD_RE = re.compile(r"[a-z0-9]{2,}")
# Facts tagged "identity" (or keyed as one of these) ride in every prompt,
# in full, regardless of query relevance — same rationale as Mark LIII's
# _IDENTITY_FIELDS: it's wrong for the assistant to have to guess or search
# for the user's own name.
_IDENTITY_KEYS = frozenset({
    "name", "preferred_name", "user_name", "pronouns", "timezone",
    "location", "city", "birthday", "job", "role",
})
_RECALL_ALL = re.compile(
    r"\b(what do you (remember|know)|who am i|about me|"
    r"(list|show|dump) (my |your )?(memory|memories|facts)|"
    r"saved facts|everything you(?:'ve| have) (stored|remembered)|"
    r"what have you (stored|remembered|saved))\b",
    re.I,
)
_STOP = frozenset({
    "a", "an", "the", "to", "of", "and", "or", "is", "it", "in", "on", "for",
    "that", "this", "you", "your", "do", "does", "did", "can", "could", "would",
    "please", "just", "want", "with", "from", "are", "was", "be", "been", "am",
    "we", "they", "them", "as", "at", "by", "if", "not", "no", "yes", "ok",
    "how", "what", "when", "where", "which", "who", "why", "me", "my", "i",
    "im", "ive", "ill", "dont", "its", "also", "some", "any", "all",
})
_WEAK = frozenset({
    "play", "open", "launch", "run", "use", "get", "set", "make", "need",
    "something", "thing", "stuff", "app", "please", "help", "turn", "put",
})
_EXPAND = {
    "spotify": ("music", "song", "songs", "playlist", "track", "album"),
    "music": ("spotify", "song", "playlist"),
    "song": ("spotify", "music"),
    "wifi": ("wireless", "ssid", "network"),
    "wireless": ("wifi",),
    "bluetooth": ("bt",),
    "bt": ("bluetooth",),
    "playnite": ("game", "games", "library"),
    "game": ("playnite", "games"),
    "games": ("playnite", "game"),
    "git": ("github", "commit", "repo", "repository"),
    "gpu": ("rtx", "nvidia", "graphics", "videocard"),
    "graphics": ("gpu", "rtx", "nvidia"),
    "nvidia": ("gpu", "rtx"),
}


def ensure_config():
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps({"facts": []}, indent=2) + "\n", encoding=ENCODING
        )


def load_facts():
    ensure_config()
    from . import atomic_io
    # Backup-aware: without this the .bak that save_facts now writes would
    # never actually be consulted, and a corrupt main file would still read
    # as "no memories" — the exact silent-total-loss this pair exists to
    # stop. Losing the newest fact beats losing every fact.
    data = atomic_io.read_json(CONFIG_FILE, default=None, expect=dict)
    if data is None:
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict) and (f.get("fact") or "").strip()]


def save_facts(facts):
    """Atomic, with a .bak fallback — see atomic_io's module docstring.

    This file is the only copy of everything the user ever asked Jarvis to
    remember, and it is fully rewritten on every single memory_save. The
    old plain write_text() meant one force-kill landing in that window
    wiped the lot, silently: load_facts() catches the parse error and
    returns [], so a destroyed memory file reads as "no memories yet".
    """
    ensure_config()
    from . import atomic_io
    atomic_io.write_json(CONFIG_FILE, {"facts": facts[-MAX_FACTS:]})


def _norm_key(key):
    if not isinstance(key, str):
        return ""
    key = key.strip().lower().replace(" ", "_")
    if not key:
        return ""
    if not _KEY_RE.match(key):
        key = re.sub(r"[^a-z0-9_\-]+", "_", key).strip("_")[:MAX_KEY_LEN]
    return key[:MAX_KEY_LEN]


def _tokens(text):
    if not text:
        return set()
    return {t for t in _WORD_RE.findall(text.lower()) if t not in _STOP}


def _expand(tokens):
    out = set(tokens)
    for t in list(tokens):
        for extra in _EXPAND.get(t, ()):
            out.add(extra)
    return out


def _fact_tokens(fact):
    key = (fact.get("key") or "")
    tags = fact.get("tags") or []
    text = fact.get("fact") or ""
    blob = " ".join([key.replace("_", " "), " ".join(str(t) for t in tags), text])
    return _tokens(blob), _tokens(key.replace("_", " ")), _tokens(" ".join(str(t) for t in tags))


def _score_fact(fact, query_tokens):
    if not query_tokens:
        return 0
    ftoks, key_toks, tag_toks = _fact_tokens(fact)
    score = 0
    key_hits = key_toks & query_tokens
    tag_hits = tag_toks & query_tokens
    if key_hits:
        score += 8 + 3 * len(key_hits)
    if tag_hits:
        score += 6 + 2 * len(tag_hits)
    overlap = ftoks & query_tokens
    strong = {t for t in overlap if t not in _WEAK and len(t) >= 3}
    weak = overlap - strong
    if strong:
        score += 4 * len(strong)
        score += sum(1 for t in strong if len(t) >= 6)
    elif weak and not key_hits and not tag_hits:
        return 0
    return score


def _format_fact_line(fact):
    key = (fact.get("key") or "").strip()
    text = (fact.get("fact") or "").strip()
    fid = (fact.get("id") or "").strip()
    prefix = f"[{key}] " if key else ""
    line = f"- {prefix}{text}"
    if fid:
        line += f"  (id:{fid})"
    return line


def _fact_label(f):
    """Short, greppable label for a fact — what goes in the overflow index so
    the model has something concrete to pass to memory_search."""
    key = (f.get("key") or "").strip()
    if key:
        return key.replace("_", " ")
    tags = f.get("tags") or []
    if tags:
        return str(tags[0])
    text = (f.get("fact") or "").strip()
    return (text[:24] + "…") if len(text) > 24 else (text or "fact")


def _is_identity_fact(f):
    return "identity" in (f.get("tags") or []) or (f.get("key") or "") in _IDENTITY_KEYS


def _render_index(overflow_labels):
    """Render the trailing 'also remembered' line from a list of labels,
    deduped and capped to PROMPT_INDEX_CHARS so the index can't itself blow
    the budget it exists to protect."""
    seen = set()
    names = []
    idx_budget = PROMPT_INDEX_CHARS
    extra = 0
    for label in overflow_labels:
        if label in seen:
            continue
        seen.add(label)
        if idx_budget - len(label) - 2 < 0:
            extra += 1
            continue
        names.append(label)
        idx_budget -= len(label) + 2
    if not names:
        return ""
    line = (
        "(Also remembered, not shown here — call memory_search with one of "
        "these to read it: " + ", ".join(names)
        + (f", +{extra} more" if extra else "") + ")"
    )
    return line


def _render_facts(facts, budget, overflow_labels=None):
    header = (
        "Relevant long-term memory (trust these over chat recap; "
        "memory_search if something is missing; memory_save / memory_forget to change):"
    )
    lines = [header]
    used = len(header)
    included = 0
    overflow_labels = list(overflow_labels or [])
    for j, f in enumerate(facts):
        line = _format_fact_line(f)
        if used + len(line) + 1 > budget:
            overflow_labels.extend(_fact_label(x) for x in facts[j:])
            break
        lines.append(line)
        used += len(line) + 1
        included += 1
    if not included and not overflow_labels:
        return ""
    index_line = _render_index(overflow_labels)
    if index_line:
        lines.append(index_line)
    return "\n".join(lines) if included or index_line else ""


def prompt_context(char_budget=None, compact=False, query="", extra_texts=None):
    """Return memory lines relevant to query, or '' if nothing matches.

    Identity facts (tag "identity", or a well-known key like name/timezone)
    always ride along in full. Everything else still has to match the query
    to be considered, then competes for the remaining budget — capped per
    primary tag (PROMPT_MAX_PER_TAG) so one chatty tag can't crowd out the
    rest — and whatever matched but didn't fit is listed by label in a
    trailing index instead of a bare count, so the model can still
    memory_search it by name.
    """
    facts = load_facts()
    if not facts:
        return ""
    budget = char_budget if char_budget is not None else (
        PROMPT_COMPACT_BUDGET if compact else PROMPT_FULL_BUDGET
    )
    extras = extra_texts or []
    blob = " ".join([query or ""] + [t for t in extras if t])
    if _RECALL_ALL.search(blob or ""):
        newest_first = list(reversed(facts))
        return _render_facts(newest_first, budget)

    identity_facts = [f for f in facts if _is_identity_fact(f)]
    qtoks = _expand(_tokens(blob))

    if not qtoks:
        if not identity_facts:
            return ""
        return _render_facts(identity_facts, budget)

    ranked = []
    for i, f in enumerate(facts):
        if _is_identity_fact(f):
            continue
        s = _score_fact(f, qtoks)
        if s <= 0:
            continue
        ranked.append((s, i, f))
    ranked.sort(key=lambda x: (-x[0], -x[1]))

    if not identity_facts and not ranked:
        return ""

    chosen = list(identity_facts)
    tag_used = {}
    overflow_labels = []
    for _s, _i, f in ranked:
        if len(chosen) - len(identity_facts) >= MAX_PROMPT_FACTS:
            overflow_labels.append(_fact_label(f))
            continue
        primary_tag = (f.get("tags") or [None])[0]
        if tag_used.get(primary_tag, 0) >= PROMPT_MAX_PER_TAG:
            overflow_labels.append(_fact_label(f))
            continue
        chosen.append(f)
        tag_used[primary_tag] = tag_used.get(primary_tag, 0) + 1

    return _render_facts(chosen, budget, overflow_labels=overflow_labels)


def tool_memory_save(args):
    args = args or {}
    fact = (args.get("fact") or args.get("text") or "").strip()
    if len(fact) < 3:
        return {"needs_clarification": True, "message": "What should I remember?"}
    if len(fact) > MAX_FACT_LEN:
        fact = fact[: MAX_FACT_LEN - 1].rstrip() + "…"
    key = _norm_key(args.get("key") or "")
    tags = args.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip().lower()[:24] for t in tags if str(t).strip()][:6]

    facts = load_facts()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if key:
        for f in facts:
            if (f.get("key") or "") == key:
                f["fact"] = fact
                f["tags"] = tags or f.get("tags") or []
                f["updated"] = now
                save_facts(facts)
                return {"ok": True, "updated": True, "id": f.get("id"), "key": key, "fact": fact}

    fid = "m_" + secrets.token_hex(4)
    entry = {"id": fid, "key": key, "fact": fact, "tags": tags, "updated": now}
    facts.append(entry)
    if len(facts) > MAX_FACTS:
        facts = facts[-MAX_FACTS:]
    save_facts(facts)
    return {"ok": True, "updated": False, "id": fid, "key": key or None, "fact": fact}


def tool_memory_forget(args):
    args = args or {}
    fid = (args.get("id") or "").strip()
    key = _norm_key(args.get("key") or "")
    query = (args.get("query") or args.get("fact") or "").strip().lower()
    if not fid and not key and len(query) < 2:
        return {
            "needs_clarification": True,
            "message": "Which memory? Pass id, key, or a short query.",
        }

    facts = load_facts()
    kept, removed = [], []
    for f in facts:
        hit = False
        if fid and f.get("id") == fid:
            hit = True
        elif key and (f.get("key") or "") == key:
            hit = True
        elif query and query in (f.get("fact") or "").lower():
            hit = True
        if hit:
            removed.append({"id": f.get("id"), "key": f.get("key"), "fact": f.get("fact")})
        else:
            kept.append(f)

    if not removed:
        return {"ok": False, "error": "No matching memory.", "hint": "memory_search to list ids."}
    save_facts(kept)
    return {"ok": True, "removed": removed, "remaining": len(kept)}


def tool_memory_search(args=None):
    args = args or {}
    query = (args.get("query") or args.get("q") or "").strip().lower()
    facts = load_facts()
    if query:
        facts = [
            f for f in facts
            if query in (f.get("fact") or "").lower()
            or query in (f.get("key") or "").lower()
            or query in " ".join(f.get("tags") or []).lower()
        ]
    compact = []
    for f in facts[-40:]:
        compact.append({
            "id": f.get("id"),
            "key": f.get("key") or None,
            "fact": f.get("fact"),
            "tags": f.get("tags") or [],
        })
    return {"showing": len(compact), "total_stored": len(load_facts()), "facts": compact}


MEMORY_TOOL_SCHEMAS = [
    {
        "name": "memory_save",
        "description": (
            "Save a durable fact to long-term memory (survives new chats and ai-clear). "
            "Use for preferences, names, hardware, 'remember that…', standing instructions. "
            "Pass key to update the same slot later (e.g. preferred_name, main_pc). "
            "Do NOT save passwords, API keys, or one-off trivia."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "One concise fact, e.g. 'Prefers dark mode in every app'."},
                "key": {"type": "string", "description": "Optional stable id like preferred_name or favorite_genre."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short tags: identity, prefs, hardware, games.",
                },
            },
            "required": ["fact"],
        },
    },
    {
        "name": "memory_forget",
        "description": "Delete a saved fact by id, key, or text query. Confirm with the user first if wiping something important.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "key": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "memory_search",
        "description": "Search long-term memories. The prompt only includes facts that matched this message — call this if you need more or the user asks what you remember and nothing was injected.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional substring. Empty lists recent facts."},
            },
            "required": [],
        },
    },
]

MEMORY_TOOLS = {
    "memory_save": tool_memory_save,
    "memory_forget": tool_memory_forget,
    "memory_search": tool_memory_search,
}
