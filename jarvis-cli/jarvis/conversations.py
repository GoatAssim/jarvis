"""Multiple, switchable conversations for 'jarvis <text>' and the web UI.

Where history.py kept one never-ending rolling log, this module keeps many
independent conversations, each in its own file under
``~/.jarvis/conversations/<id>.json``, with a lightweight index
(``index.json``) for fast listing and search without loading every full
transcript.

Exactly one conversation is "current" on disk (``current_conversation.json``)
— that's what a plain `jarvis <text>` call continues when no other id is
specified. The web UI instead sends an explicit conversation id with every
ask (via the JARVIS_CONVERSATION_ID env var — see cli.py's handle_ai_prompt),
since each browser tab manages its own active conversation independently of
whatever's "current" for the CLI.

Two very different amounts of context come out of here, on purpose:
  - conversation_messages() gives the *current* conversation's own recent
    turns in real detail (still capped — a recap of older turns plus the
    last several verbatim) — this is the "heavy, but not everything" side.
  - other_conversations_context() gives every *other* conversation only its
    title and a one-line gist — never its actual messages — so Jarvis can at
    most vaguely place "we talked about X before" without other chats
    quietly leaking detail into an unrelated one. That's the "really really
    soft" side.
"""

import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONV_DIR = JARVIS_DIR / "conversations"
INDEX_FILE = CONV_DIR / "index.json"
CURRENT_FILE = JARVIS_DIR / "current_conversation.json"
ENCODING = "utf-8"

MAX_STORED_EXCHANGES = 60
DEFAULT_TITLE = "New Conversation"
MAX_TITLE_LEN = 60
MAX_SOFT_CONTEXT_LEN = 220

CONTEXT_EXCHANGES = 10
CONTEXT_CHAR_BUDGET = 4800
RECAP_EXCHANGES = 16
RECAP_CHAR_BUDGET = 1400
MAX_USER_CHARS = 500
MAX_ASSISTANT_CHARS = 700

# Ids are secrets.token_hex(8) — 16 lowercase hex chars. Anything used to
# build a path on disk (env var, CLI argv, HTTP body) is checked against
# this before it ever touches the filesystem, so a stray "../../x" can't
# escape CONV_DIR.
_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")


def is_valid_id(conv_id):
    return isinstance(conv_id, str) and bool(_ID_RE.match(conv_id))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conv_path(conv_id):
    return CONV_DIR / f"{conv_id}.json"


# ---- low-level file IO ------------------------------------------------------

def _load_index():
    if not INDEX_FILE.exists():
        return []
    try:
        data = json.loads(INDEX_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_index(items):
    CONV_DIR.mkdir(parents=True, exist_ok=True)
    try:
        INDEX_FILE.write_text(json.dumps(items, indent=2) + "\n", encoding=ENCODING)
    except OSError as e:
        print(f"Warning: couldn't save conversation index: {e}", file=sys.stderr)


def _load_conv(conv_id):
    if not is_valid_id(conv_id):
        return None
    path = _conv_path(conv_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _save_conv(record):
    CONV_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _conv_path(record["id"]).write_text(
            json.dumps(record, indent=2) + "\n", encoding=ENCODING
        )
    except OSError as e:
        print(f"Warning: couldn't save conversation: {e}", file=sys.stderr)


def _index_entry(record):
    return {
        "id": record["id"],
        "title": record.get("title") or DEFAULT_TITLE,
        "soft_context": record.get("soft_context") or "",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "exchange_count": len(record.get("exchanges") or []),
    }


def _upsert_index(record):
    items = _load_index()
    entry = _index_entry(record)
    for i, it in enumerate(items):
        if it.get("id") == record["id"]:
            items[i] = entry
            break
    else:
        items.append(entry)
    _save_index(items)


def _remove_from_index(conv_id):
    items = [it for it in _load_index() if it.get("id") != conv_id]
    _save_index(items)


# ---- current pointer (plain-CLI convenience only) ---------------------------

def get_current_id(auto_create=True):
    """The CLI's on-disk 'active' conversation. Web asks instead pass an
    explicit id via JARVIS_CONVERSATION_ID and never touch this pointer."""
    conv_id = None
    try:
        data = json.loads(CURRENT_FILE.read_text(encoding=ENCODING))
        conv_id = data.get("id") if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        conv_id = None
    if conv_id and _load_conv(conv_id):
        return conv_id
    if not auto_create:
        return None
    return new_conversation()


def set_current(conv_id):
    if not is_valid_id(conv_id):
        return
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CURRENT_FILE.write_text(json.dumps({"id": conv_id}) + "\n", encoding=ENCODING)
    except OSError as e:
        print(f"Warning: couldn't save current conversation pointer: {e}", file=sys.stderr)


# ---- CRUD --------------------------------------------------------------------

def new_conversation(title=None, make_current=True):
    """Create a brand-new, empty conversation and return its id. This is
    what both 'opening the page' (web — every fresh page load starts one)
    and the CLI's own first-ever use (or an explicit `jarvis conv-new`)
    call to start a clean slate.

    Deliberately NOT added to the index here (see _upsert_index below) —
    a conversation only earns a place in the browsable history once it
    has a real exchange in it (append_exchange does the indexing then).
    Without this, every page load and every "+ New" click would leave a
    permanent, empty "No messages yet" entry in the sidebar even if the
    user never actually said anything — this is exactly the clutter this
    split is meant to avoid. The record is still written to disk (so
    get_conversation/conv-show and append_exchange's own lookup work
    immediately) — it's just invisible to list_conversations() until it's
    no longer empty. See _sweep_empty_orphans for what eventually cleans
    those never-used files off disk.
    """
    _sweep_empty_orphans()
    conv_id = secrets.token_hex(8)
    now = _now()
    record = {
        "id": conv_id,
        "title": (title or "").strip()[:MAX_TITLE_LEN] or DEFAULT_TITLE,
        "soft_context": "",
        "created_at": now,
        "updated_at": now,
        "exchanges": [],
    }
    _save_conv(record)
    if make_current:
        set_current(conv_id)
    return conv_id


# A brand-new conversation's on-disk file (see new_conversation) is only
# ever indexed once append_exchange gives it a first real message. Most of
# the time that happens within minutes, one way or another — either the
# user says something, or they move on and the tab/session is forgotten.
# This sweep clears out the latter case: files that never made it into the
# index and are old enough that they're clearly abandoned rather than a
# conversation someone still has open and is about to type into. It's
# intentionally conservative (a full hour, and it only ever touches files
# that are (a) not in the index and (b) genuinely have zero exchanges) so
# it can never race-delete something actually in progress.
_ORPHAN_SWEEP_AGE_SECONDS = 60 * 60


def _sweep_empty_orphans():
    if not CONV_DIR.exists():
        return
    try:
        indexed_ids = {it.get("id") for it in _load_index()}
        now = datetime.now(timezone.utc)
        for path in CONV_DIR.glob("*.json"):
            if path.name == INDEX_FILE.name or path.stem in indexed_ids:
                continue
            try:
                data = json.loads(path.read_text(encoding=ENCODING))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            if not isinstance(data, dict) or (data.get("exchanges") or []):
                continue
            try:
                created = datetime.fromisoformat(data.get("created_at") or "")
            except ValueError:
                continue
            if (now - created).total_seconds() < _ORPHAN_SWEEP_AGE_SECONDS:
                continue
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        # Best-effort housekeeping only — a failed sweep should never stop
        # a new conversation from being created.
        pass


def get_conversation(conv_id):
    return _load_conv(conv_id)


def list_conversations(query=None):
    """Every conversation's lightweight index entry, most recently updated
    first. `query` filters on title/soft_context/id (case-insensitive
    substring) — this is what backs the web UI's conversation search."""
    items = _load_index()
    items.sort(key=lambda it: it.get("updated_at") or "", reverse=True)
    q = (query or "").strip().lower()
    if not q:
        return items

    def matches(it):
        haystack = " ".join(
            [it.get("title") or "", it.get("soft_context") or "", it.get("id") or ""]
        ).lower()
        return q in haystack

    return [it for it in items if matches(it)]


def delete_conversation(conv_id):
    if not is_valid_id(conv_id):
        return False
    path = _conv_path(conv_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    _remove_from_index(conv_id)
    try:
        data = json.loads(CURRENT_FILE.read_text(encoding=ENCODING))
        if isinstance(data, dict) and data.get("id") == conv_id:
            CURRENT_FILE.unlink(missing_ok=True)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        pass
    return True


def update_meta(conv_id, title=None, soft_context=None):
    """Sets the AI-generated title and/or one-line gist. Both are
    best-effort cosmetic metadata — a failure here should never break an
    actual ask (see ai_client._maybe_update_title)."""
    record = _load_conv(conv_id)
    if not record:
        return False
    if title is not None and title.strip():
        record["title"] = title.strip()[:MAX_TITLE_LEN]
    if soft_context is not None and soft_context.strip():
        record["soft_context"] = soft_context.strip()[:MAX_SOFT_CONTEXT_LEN]
    record["updated_at"] = _now()
    _save_conv(record)
    _upsert_index(record)
    return True


def append_exchange(conv_id, user_text, jarvis_text, provider, extras=None):
    """Adds one turn to a conversation and returns the new exchange count
    (used by the caller to decide whether it's time to (re)generate a
    title). Recreates the record defensively if it's somehow missing —
    an env var or CLI arg holding a stale/foreign id shouldn't crash an
    otherwise-successful ask.

    `extras`, if given, is the list of non-text thread items (screenshots,
    downloads, organize_json results, resolved confirmations) that
    happened during this turn — see ai_client._extras_from_runs. Saved
    verbatim alongside the exchange so the web UI can replay them after a
    real page reload, not just for as long as its in-memory state lasts.
    """
    if not is_valid_id(conv_id):
        return 0
    record = _load_conv(conv_id) or {
        "id": conv_id,
        "title": DEFAULT_TITLE,
        "soft_context": "",
        "created_at": _now(),
        "updated_at": _now(),
        "exchanges": [],
    }
    exchange = {
        "ts": _now(),
        "user": user_text,
        "jarvis": jarvis_text,
        "provider": provider,
    }
    if extras:
        exchange["extras"] = extras
    record.setdefault("exchanges", []).append(exchange)
    record["exchanges"] = record["exchanges"][-MAX_STORED_EXCHANGES:]
    record["updated_at"] = _now()
    _save_conv(record)
    _upsert_index(record)
    return len(record["exchanges"])


def drop_from_user(conv_id, user_text):
    """Remove the last exchange matching user_text and everything after it
    — used when the web UI redoes a prompt, same as history.py's version."""
    record = _load_conv(conv_id)
    if not record:
        return False
    target = (user_text or "").strip()
    if not target:
        return False
    exchanges = record.get("exchanges") or []
    idx = None
    for i in range(len(exchanges) - 1, -1, -1):
        if (exchanges[i].get("user") or "").strip() == target:
            idx = i
            break
    if idx is None:
        return False
    record["exchanges"] = exchanges[:idx]
    record["updated_at"] = _now()
    _save_conv(record)
    _upsert_index(record)
    return True


def clear(conv_id):
    """Wipes this conversation's messages in place (same id, same title) —
    what the web UI's 'Clear' button does. Starting a genuinely new
    conversation is new_conversation() instead."""
    record = _load_conv(conv_id)
    if not record:
        return False
    record["exchanges"] = []
    record["updated_at"] = _now()
    _save_conv(record)
    _upsert_index(record)
    return True


# ---- prompt-facing helpers ---------------------------------------------------

def _truncate(text, max_len):
    if max_len <= 0 or len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "\u2026"


def _compact_args(args, max_len=40):
    """Name-and-key-argument only, no result payloads — a short 'k=v, k=v'
    rendering of a tool's arguments dict for recap lines."""
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    for k, v in args.items():
        parts.append(f"{k}={v}")
    return _truncate(", ".join(parts), max_len)


def _extras_recap_fragment(extras):
    """Turns an exchange's stored `extras` (see ai_client._extras_from_runs)
    into a short structured 'ran x(...), y(...)' fragment for the recap —
    name-and-key-argument only, no result payloads. Returns "" when there's
    nothing to summarize, so callers can append it unconditionally."""
    if not extras:
        return ""
    calls = []
    for extra in extras:
        etype = extra.get("type")
        data = extra.get("data") or {}
        if etype == "confirm":
            tool = data.get("tool") or "tool"
            calls.append(f"{tool}({_compact_args(data.get('arguments'))})")
        elif etype == "screenshot":
            calls.append(f"take_screenshot({data.get('filename') or ''})")
        elif etype == "organizeJson":
            calls.append(f"organize_json({data.get('targetPath') or ''})")
        elif etype == "download":
            label = data.get("title") or data.get("filename") or ""
            calls.append(f"ytdl_download({label})")
    if not calls:
        return ""
    return " [recap] ran " + ", ".join(calls)


def conversation_messages(
    conv_id,
    max_exchanges=None,
    char_budget=None,
    recap_exchanges=None,
    recap_budget=None,
):
    """The 'heavy' side: prior turns of THIS SAME conversation, as real
    messages (not everything — a compressed recap of older turns, then
    recent turns verbatim, both capped), exactly like history.py's old
    conversation_messages but scoped to one conversation."""
    recent_n = CONTEXT_EXCHANGES if max_exchanges is None else max_exchanges
    recent_budget = CONTEXT_CHAR_BUDGET if char_budget is None else char_budget
    recap_n = RECAP_EXCHANGES if recap_exchanges is None else recap_exchanges
    recap_lim = RECAP_CHAR_BUDGET if recap_budget is None else recap_budget

    record = _load_conv(conv_id)
    exchanges = (record or {}).get("exchanges") or []
    if not exchanges:
        return []

    recent_src = exchanges[-recent_n:] if recent_n else []
    older_src = exchanges[:-recent_n][-recap_n:] if recap_n and len(exchanges) > recent_n else []

    messages = []
    recap_lines = []
    used_r = 0
    for ex in reversed(older_src):
        user_text = _truncate((ex.get("user") or "").strip(), 90)
        assistant_text = _truncate((ex.get("jarvis") or "").strip(), 110)
        if not user_text:
            continue
        line = f"- User: {user_text} \u2192 You: {assistant_text}"
        line += _extras_recap_fragment(ex.get("extras"))
        if used_r + len(line) > recap_lim:
            break
        recap_lines.append(line)
        used_r += len(line)
    recap_lines.reverse()
    if recap_lines:
        messages.append({
            "role": "user",
            "content": "Earlier in this same conversation (compressed):\n" + "\n".join(recap_lines),
        })

    used = 0
    recent = []
    for ex in recent_src:
        user_text = _truncate((ex.get("user") or "").strip(), MAX_USER_CHARS)
        assistant_text = _truncate((ex.get("jarvis") or "").strip(), MAX_ASSISTANT_CHARS)
        if not user_text or not assistant_text:
            continue
        pair_len = len(user_text) + len(assistant_text)
        if used + pair_len > recent_budget:
            remaining = recent_budget - used
            if remaining < 80 or recent:
                break
            if len(user_text) > remaining // 2:
                user_text = _truncate(user_text, remaining // 2)
            remaining -= len(user_text)
            assistant_text = _truncate(assistant_text, max(remaining, 40))
            pair_len = len(user_text) + len(assistant_text)
        recent.append({"role": "user", "content": user_text})
        recent.append({"role": "assistant", "content": assistant_text})
        used += pair_len

    messages.extend(recent)
    return messages


def other_conversations_context(exclude_id, limit=5):
    """The 'really really soft' side: just the title + one-line gist of a
    few OTHER recent conversations — never their actual message content —
    so Jarvis can at most vaguely recall 'we talked about X before'. Skips
    conversations that don't have a gist yet (i.e. too new to have one)."""
    items = [
        it for it in list_conversations()
        if it.get("id") != exclude_id and (it.get("soft_context") or "").strip()
    ][:limit]
    if not items:
        return ""
    lines = ["Other recent conversations you've had with this user (gist only, not full detail):"]
    for it in items:
        title = it.get("title") or DEFAULT_TITLE
        gist = it.get("soft_context") or ""
        lines.append(f"- {title}: {gist}")
    lines.append(
        "Only bring one of these up if it's actually relevant \u2014 don't volunteer old "
        "topics unprompted."
    )
    return "\n".join(lines)