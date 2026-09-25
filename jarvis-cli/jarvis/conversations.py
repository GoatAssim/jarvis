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
import os
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
        return _rebuild_index()
    data, recovered = _read_json_with_backup(INDEX_FILE, list)
    if data is None:
        # Both copies unreadable. The index is pure derived data — every
        # field in it also lives in the conversation files — so rebuild it
        # rather than returning [] and making every conversation on disk
        # invisible forever, which is what used to happen.
        print("Note: conversation index was corrupt — rebuilding from disk.",
              file=sys.stderr)
        return _rebuild_index()
    if recovered:
        print("Note: recovered the conversation index from backup.", file=sys.stderr)
    return data


def _rebuild_index():
    """Reconstruct the index by scanning the conversation files.

    Only conversations with at least one exchange are indexed, matching
    new_conversation()'s rule that an empty conversation stays invisible
    until it has a real message.
    """
    if not CONV_DIR.exists():
        return []
    items = []
    for path in sorted(CONV_DIR.glob("*.json")):
        if path.name == INDEX_FILE.name:
            continue
        data, _ = _read_json_with_backup(path, dict)
        if not isinstance(data, dict) or not data.get("id"):
            continue
        if not (data.get("exchanges") or []):
            continue
        items.append(_index_entry(data))
    items.sort(key=lambda it: it.get("updated_at") or "", reverse=True)
    if items:
        try:
            _atomic_write(INDEX_FILE, json.dumps(items, indent=2) + "\n")
        except OSError:
            pass
    return items


def _save_index(items):
    CONV_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write(INDEX_FILE, json.dumps(items, indent=2) + "\n")
    except OSError as e:
        print(f"Warning: couldn't save conversation index: {e}", file=sys.stderr)


def _load_conv(conv_id):
    if not is_valid_id(conv_id):
        return None
    path = _conv_path(conv_id)
    data, recovered = _read_json_with_backup(path, dict)
    if data is None:
        return None
    if recovered:
        # Say so. A silently-restored older copy is how "it lost my last
        # message" becomes an unexplainable mystery instead of a known,
        # bounded loss of exactly one turn.
        print(f"Note: recovered conversation {conv_id} from backup "
              f"(the main file was corrupt).", file=sys.stderr)
    return data


def _atomic_write(path, text):
    """Write via temp file + os.replace, keeping the previous contents as a
    .bak sibling.

    THIS IS THE FIX FOR "I aborted a message and the whole conversation
    vanished." Both of these used a plain write_text(), which truncates the
    target before writing — so a process killed mid-write left a HALF a
    JSON file behind. _load_conv catches JSONDecodeError and returns None,
    so a truncated file didn't look corrupt, it looked like the
    conversation had never existed. Same for the index, except there one
    torn write hid EVERY conversation at once.

    That kill is not hypothetical or rare: the web UI's Stop button calls
    killTree(), which on Windows is `taskkill /T /F` — an unconditional
    force kill with no signal handler able to intervene. Every Stop press
    on Windows races this write.

    os.replace is atomic on POSIX and on Windows (MoveFileEx with
    REPLACE_EXISTING), so a reader sees either the old file or the new one,
    never a torn one. The fsync before it is what makes that hold across a
    power loss rather than just a process kill.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding=ENCODING) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    # Keep the last known-good copy. Cheap insurance: the failure this
    # guards against already happened once and silently destroyed data.
    if path.exists():
        try:
            backup = path.with_suffix(path.suffix + ".bak")
            os.replace(str(path), str(backup))
        except OSError:
            pass
    os.replace(str(tmp), str(path))


def _read_json_with_backup(path, expect):
    """Load JSON from `path`, falling back to its .bak on corruption.

    Returns (data, recovered). `expect` is the type required — anything
    else is treated as corruption, since a conversation that deserializes
    to a list is no more usable than one that doesn't parse at all.
    """
    for candidate, is_backup in ((path, False),
                                 (path.with_suffix(path.suffix + ".bak"), True)):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding=ENCODING))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if isinstance(data, expect):
            return data, is_backup
    return None, False


def _save_conv(record):
    CONV_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write(_conv_path(record["id"]),
                      json.dumps(record, indent=2) + "\n")
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
        "origin": record.get("origin") or "",
        "origin_detail": record.get("origin_detail") or "",
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
    # Backup-aware, to match set_current's atomic write: reading the main
    # file directly meant a torn pointer read as "none", and the next CLI
    # message silently opened a NEW conversation instead of continuing the
    # one the user was in.
    data, _ = _read_json_with_backup(CURRENT_FILE, dict)
    conv_id = data.get("id") if isinstance(data, dict) else None
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
        # Atomic too: a torn pointer file reads as "no current
        # conversation", so get_current_id() silently starts a NEW one and
        # the user's next CLI message lands somewhere they never see.
        _atomic_write(CURRENT_FILE, json.dumps({"id": conv_id}) + "\n")
    except OSError as e:
        print(f"Warning: couldn't save current conversation pointer: {e}", file=sys.stderr)


# ---- CRUD --------------------------------------------------------------------

def new_conversation(title=None, make_current=True, origin="", origin_detail=""):
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
        # Where this conversation came from: "" (plain web/CLI), "discord",
        # "instagram", "scheduler". Carried into the index (see
        # _index_entry) so the Logs viewer can label a conversation without
        # opening its file — a chat-bot thread and a scheduled job look
        # exactly like a normal ask once they're just exchanges on disk,
        # and "why is there a conversation I don't remember having" is a
        # genuinely confusing thing to hit in the sidebar.
        "origin": (origin or "").strip()[:32],
        "origin_detail": (origin_detail or "").strip()[:120],
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


def last_assistant_reply(conv_id):
    """The most recent exchange's `jarvis` text for this conversation, or
    None if there isn't one (no conversation, no exchanges yet, or the
    last exchange has no reply recorded — e.g. an abandoned turn). Used by
    ai_client.ask() (master plan F.10) to recognize when the CURRENT
    user_text is Jarvis's own previous reply pasted back verbatim, so
    routing can strip it out instead of scoring the assistant's own
    closing line (e.g. "Let me know if you'd like me to handle it...")
    against the user's actual instruction."""
    if not is_valid_id(conv_id):
        return None
    record = _load_conv(conv_id)
    if not record:
        return None
    exchanges = record.get("exchanges") or []
    if not exchanges:
        return None
    return exchanges[-1].get("jarvis") or None


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
    from . import route_stickiness, skill_stickiness
    route_stickiness.clear_sticky(conv_id)
    skill_stickiness.unload_all(conv_id)  # a fresh conversation shouldn't inherit an old one's force-loaded skill either
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


def begin_exchange(conv_id, user_text, console_turn=None):
    """Write the user's half of a turn to disk BEFORE the model is called.

    This is the fix for "I sent a message, aborted it, and the conversation
    was empty". Persistence used to happen exactly once, in append_exchange,
    from inside `if result.ok:` in ai_client.ask() — so anything that stopped
    the process before that line (the web UI's Stop button, which killTree()s
    the child; every provider failing; a crash) threw the user's message away
    entirely. On a first message that also meant no title was ever generated,
    so the whole conversation looked like it had never happened.

    Now the turn lands immediately, marked `pending`, and is upgraded in
    place by complete_exchange() or downgraded by abandon_exchange(). A
    pending exchange is deliberately still a real, visible exchange — a
    reload shows what you asked, even though nothing answered it.

    `console_turn`, if given, is the id `console_store.begin_turn()` just
    minted for this same turn (K.2.5.2). It's saved on the pending exchange
    so `_reclaim_stale_pending()` can still point at that turn's console
    lines even after a hard kill that skipped every other cleanup path —
    the turn id is the one thing a brand-new process couldn't otherwise
    recover, since `console_store`'s own bookkeeping is in-process state
    that died with the old process.

    Returns the index of the pending exchange, or -1 if it couldn't be
    written (a bad id, an unwritable disk) — callers treat that as "carry on
    without persistence", never as a reason to fail the ask.
    """
    if not is_valid_id(conv_id):
        return -1
    record = _load_conv(conv_id) or {
        "id": conv_id,
        "title": DEFAULT_TITLE,
        "soft_context": "",
        "created_at": _now(),
        "updated_at": _now(),
        "exchanges": [],
    }
    # Reclaim anything left pending by a PREVIOUS process before adding
    # ours. A pending turn is only ever resolved in-process, by
    # complete_exchange or by the signal handler's abandon_exchange — so a
    # hard kill (Windows' Stop button is `taskkill /F`, which no handler
    # can intercept) strands one forever. Without this they accumulate:
    # every killed ask leaves a ghost turn that renders as permanently
    # unanswered and is silently dropped from prompt history.
    _reclaim_stale_pending(record)
    exchange = {
        "ts": _now(),
        "user": user_text,
        "jarvis": "",
        "provider": None,
        "pending": True,
    }
    if console_turn:
        exchange["consoleTurn"] = console_turn
    record.setdefault("exchanges", []).append(exchange)
    record["exchanges"] = record["exchanges"][-MAX_STORED_EXCHANGES:]
    record["updated_at"] = _now()
    _save_conv(record)
    _upsert_index(record)
    return len(record["exchanges"]) - 1


def _reclaim_stale_pending(record):
    """Downgrade any still-pending exchange to `interrupted`.

    Called at the start of begin_exchange, which is safe because a pending
    turn belonging to a LIVE ask in another process would mean two asks
    writing the same conversation concurrently — already unsupported, and
    the reclaim leaves the user's text untouched either way. Returns how
    many were reclaimed.

    K.2.5.2: a pending exchange carrying a `consoleTurn` (see
    begin_exchange) gets a real `consoleRef` extra pointing at whatever that
    turn's console store actually holds, the same shape
    `console_store.end_turn()` already builds for the in-process abandon
    paths (`ai_client.abandon_pending_turn`) — so a process that died too
    hard for even the signal handler to run (SIGKILL, OOM, power loss) still
    leaves a reclaimed exchange that can show what ran, not just "you asked
    this and nothing answered." The console data itself was never at risk
    either way — it's written line-by-line as it happens — this only fixes
    the reclaimed exchange record's own extras missing the pointer to it.
    Imported lazily to avoid a module-level import cycle: console_store.py
    already imports this module (conversations.py) for `is_valid_id`.
    """
    reclaimed = 0
    for exchange in record.get("exchanges") or []:
        if not exchange.get("pending"):
            continue
        exchange.pop("pending", None)
        exchange["jarvis"] = exchange.get("jarvis") or ""
        exchange.setdefault("interrupted", "interrupted (process ended)")
        console_turn = exchange.pop("consoleTurn", None)
        if console_turn and not exchange.get("extras"):
            try:
                from . import console_store
                lines = console_store.line_count_for_turn(record.get("id"), console_turn)
            except Exception:  # noqa: BLE001 — a missing pointer beats a crashed reclaim
                lines = 0
            if lines:
                exchange["extras"] = [{"type": "consoleRef", "data": {"turn": console_turn, "lines": lines}}]
        reclaimed += 1
    return reclaimed


def _finish_pending(record, user_text, patch):
    """Apply `patch` to the newest pending exchange, preferring one whose
    user text matches. Falls back to appending a fresh exchange if there's
    no pending one to claim — begin_exchange() may have failed to write, and
    losing the reply because of that would be a strictly worse bug than the
    one this whole mechanism exists to fix."""
    exchanges = record.setdefault("exchanges", [])
    target = (user_text or "").strip()
    for i in range(len(exchanges) - 1, -1, -1):
        if not exchanges[i].get("pending"):
            continue
        if target and (exchanges[i].get("user") or "").strip() != target:
            continue
        exchanges[i].update(patch)
        exchanges[i].pop("pending", None)
        # K.2.5.2's consoleTurn is bookkeeping for a hard-kill reclaim that
        # never happened here — this turn is resolving normally, through
        # complete_exchange() or abandon_exchange(), both of which already
        # build their own consoleRef (from console_store.end_turn(), while
        # the turn's still active in-process) into `patch["extras"]` when
        # there's console data to point at. Leaving the raw turn id behind
        # on top of that would just be dead clutter on every single
        # exchange, forever, not a second safety net.
        exchanges[i].pop("consoleTurn", None)
        return i
    exchange = {"ts": _now(), "user": user_text}
    exchange.update(patch)
    exchanges.append(exchange)
    record["exchanges"] = exchanges[-MAX_STORED_EXCHANGES:]
    return len(record["exchanges"]) - 1


def complete_exchange(conv_id, user_text, jarvis_text, provider, extras=None):
    """Fill in the reply half of a turn started by begin_exchange().

    Same return value as append_exchange (the new exchange count), so
    ai_client's title-generation logic is unchanged.
    """
    if not is_valid_id(conv_id):
        return 0
    record = _load_conv(conv_id) or {
        "id": conv_id, "title": DEFAULT_TITLE, "soft_context": "",
        "created_at": _now(), "updated_at": _now(), "exchanges": [],
    }
    patch = {"ts": _now(), "jarvis": jarvis_text, "provider": provider}
    if extras:
        patch["extras"] = extras
    _finish_pending(record, user_text, patch)
    record["updated_at"] = _now()
    _save_conv(record)
    _upsert_index(record)
    return len(record["exchanges"])


def abandon_exchange(conv_id, user_text=None, reason="interrupted", extras=None):
    """Mark a pending turn as never-answered instead of leaving it pending
    forever. Called from cli.py's signal handler (the Stop button) and from
    ai_client when every provider fails.

    The user's message stays exactly as they typed it; only the reply half
    records what went wrong. `extras` is still saved when present, because
    a turn aborted halfway may well have already taken a screenshot or
    written a file, and those happened whether or not a reply arrived.
    """
    if not is_valid_id(conv_id):
        return False
    record = _load_conv(conv_id)
    if not record:
        # No record at all means begin_exchange never managed to write one.
        # Losing the user's message on top of losing their answer is the
        # worse of the two failures, so synthesize the record rather than
        # bailing — this is the same reasoning _finish_pending's append
        # fallback already documents.
        if not (user_text or "").strip():
            return False
        record = {
            "id": conv_id, "title": DEFAULT_TITLE, "soft_context": "",
            "created_at": _now(), "updated_at": _now(), "exchanges": [],
        }
    exchanges = record.get("exchanges") or []
    # Two cases used to be conflated under a single `return False` when
    # nothing was pending:
    #
    #   (a) the turn was never recorded at all — begin_exchange failed, or
    #       no record existed. Returning False here threw the user's typed
    #       message away entirely, which is the exact bug the whole pending
    #       mechanism exists to prevent.
    #   (b) the turn is ALREADY recorded and resolved, and this is a second
    #       abandon for it. That genuinely is a no-op — and it happens in
    #       practice: the signal handler can fire twice (SIGINT then
    #       SIGTERM), and ask()'s all-providers-failed path abandons too.
    #
    # So distinguish them instead of picking one. Only (b) short-circuits.
    target = (user_text or "").strip()
    if not any(e.get("pending") for e in exchanges):
        last = exchanges[-1] if exchanges else None
        already_recorded = (
            last is not None
            and (not target or (last.get("user") or "").strip() == target)
            and not last.get("pending")
        )
        if already_recorded:
            return False
    patch = {"ts": _now(), "jarvis": "", "provider": None, "interrupted": reason}
    if extras:
        patch["extras"] = extras
    _finish_pending(record, user_text, patch)
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
    from . import route_stickiness
    route_stickiness.clear_sticky(conv_id)
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
    # An exchange with no reply (pending right now, or abandoned when the
    # user hit Stop) is real history the UI should show, but it must never
    # reach a provider: an assistant message with empty content is a hard
    # 400 on Anthropic and silently degrades the others. Filtered here, at
    # the single point where exchanges become prompt messages, rather than
    # at each of the several places that write them.
    exchanges = [e for e in exchanges if (e.get("jarvis") or "").strip()]
    if not exchanges:
        return []

    recent_src = exchanges[-recent_n:] if recent_n else []
    older_src = exchanges[:-recent_n][-recap_n:] if recap_n and len(exchanges) > recent_n else []

    messages = []
    recap_text = None
    if older_src:
        # Prefer a real model's summary of the older turns over mechanical
        # truncation — see history_summarizer.py's module docstring for
        # why. This is a best-effort round trip (cached per unchanged
        # older_src, so it only actually runs again when a new exchange
        # rolls into the older window): any failure (no eligible
        # provider, network error, bad response) returns None here and we
        # fall straight through to the exact old truncate-and-join recap
        # below, so a summarizer outage can never break an ask().
        from . import history_summarizer
        try:
            ai_recap = history_summarizer.summarize_older_exchanges(conv_id, older_src)
        except Exception:
            ai_recap = None
        if ai_recap:
            recap_text = "Earlier in this same conversation (summarized by another model):\n" + ai_recap

    if recap_text is None:
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
            recap_text = "Earlier in this same conversation (compressed):\n" + "\n".join(recap_lines)

    if recap_text:
        messages.append({"role": "user", "content": recap_text})

    # Walk the recent window NEWEST-first so the budget is spent on the most
    # recent turns before the oldest ones, then reverse back to chronological
    # order. Building this forward (oldest-of-the-window first) meant a few
    # verbose exchanges near the start of the window could exhaust
    # recent_budget before the loop ever reached the most recent turn —
    # i.e. "what did you just say" could get silently dropped while older,
    # already-recapped-adjacent turns took the budget instead. See the
    # 2026-09-14 conversation about this.
    used = 0
    kept_pairs = []
    for ex in reversed(recent_src):
        user_text = _truncate((ex.get("user") or "").strip(), MAX_USER_CHARS)
        assistant_text = _truncate((ex.get("jarvis") or "").strip(), MAX_ASSISTANT_CHARS)
        if not user_text or not assistant_text:
            continue
        pair_len = len(user_text) + len(assistant_text)
        if used + pair_len > recent_budget:
            remaining = recent_budget - used
            if remaining < 80 or kept_pairs:
                break
            if len(user_text) > remaining // 2:
                user_text = _truncate(user_text, remaining // 2)
            remaining -= len(user_text)
            assistant_text = _truncate(assistant_text, max(remaining, 40))
            pair_len = len(user_text) + len(assistant_text)
        kept_pairs.append((user_text, assistant_text))
        used += pair_len

    recent = []
    for user_text, assistant_text in reversed(kept_pairs):
        recent.append({"role": "user", "content": user_text})
        recent.append({"role": "assistant", "content": assistant_text})

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