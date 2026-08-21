"""Durable long-term memory for Jarvis (~/.jarvis/memory.json).

Conversation history is a short rolling chat log. This file is the permanent
notebook: names, preferences, hardware, 'remember that…' facts.

Facts are NOT dumped into every prompt. Each ask retrieves only facts that
look relevant to the user message (and recent user turns). Explicit 'what do
you remember' style questions load as many as the budget allows. The model
can still memory_search if retrieval misses.
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

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_WORD_RE = re.compile(r"[a-z0-9]{2,}")
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
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict) and (f.get("fact") or "").strip()]


def save_facts(facts):
    ensure_config()
    CONFIG_FILE.write_text(
        json.dumps({"facts": facts[-MAX_FACTS:]}, indent=2) + "\n",
        encoding=ENCODING,
    )


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


def _render_facts(facts, budget, omitted=0):
    header = (
        "Relevant long-term memory (trust these over chat recap; "
        "memory_search if something is missing; memory_save / memory_forget to change):"
    )
    lines = [header]
    used = len(header)
    included = 0
    for f in facts:
        line = _format_fact_line(f)
        if used + len(line) + 1 > budget:
            omitted += len(facts) - included
            break
        lines.append(line)
        used += len(line) + 1
        included += 1
    if omitted > 0:
        lines.append(f"({omitted} other facts not shown — memory_search if needed.)")
    return "\n".join(lines) if included else ""


def prompt_context(char_budget=None, compact=False, query="", extra_texts=None):
    """Return memory lines relevant to query, or '' if nothing matches."""
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
        return _render_facts(newest_first, budget, omitted=0)

    qtoks = _expand(_tokens(blob))
    if not qtoks:
        return ""

    ranked = []
    for i, f in enumerate(facts):
        s = _score_fact(f, qtoks)
        if s <= 0:
            continue
        ranked.append((s, i, f))
    if not ranked:
        return ""
    ranked.sort(key=lambda x: (-x[0], -x[1]))
    chosen = [f for _, _, f in ranked[:MAX_PROMPT_FACTS]]
    omitted = max(0, len(ranked) - len(chosen))
    return _render_facts(chosen, budget, omitted=omitted)


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
