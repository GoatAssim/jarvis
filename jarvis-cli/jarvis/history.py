"""Rolling conversation memory for 'jarvis <text>'.

Every invocation of jarvis is a brand-new process — there's no long-running
server holding conversation state in memory (see cli.py's own notes on this
philosophy). This file on disk is what gives the AI continuity between one
"jarvis ..." call and the next: recent turns are sent as real
user/assistant messages, and older turns are compressed into a short recap
so the same conversation stays coherent without dumping the whole log.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
HISTORY_FILE = JARVIS_DIR / "conversation_history.json"
ENCODING = "utf-8"

MAX_STORED_EXCHANGES = 60
CONTEXT_EXCHANGES = 10
CONTEXT_CHAR_BUDGET = 4800
RECAP_EXCHANGES = 16
RECAP_CHAR_BUDGET = 1400
MAX_USER_CHARS = 500
MAX_ASSISTANT_CHARS = 700


def _load():
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    exchanges = data.get("exchanges") if isinstance(data, dict) else None
    return exchanges if isinstance(exchanges, list) else []


def _save(exchanges):
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    trimmed = exchanges[-MAX_STORED_EXCHANGES:]
    try:
        HISTORY_FILE.write_text(
            json.dumps({"exchanges": trimmed}, indent=2) + "\n", encoding=ENCODING
        )
    except OSError as e:
        print(f"Warning: couldn't save conversation history: {e}", file=sys.stderr)


def append_exchange(user_text, jarvis_text, provider):
    exchanges = _load()
    exchanges.append({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user": user_text,
        "jarvis": jarvis_text,
        "provider": provider,
    })
    _save(exchanges)


def clear():
    _save([])


def drop_from_user(user_text):
    """Remove the last exchange matching user_text and everything after it.

    Used when the web UI redoes a prompt so the discarded reply is not sent
    back to the model on the next ask.
    """
    target = (user_text or "").strip()
    if not target:
        return False
    exchanges = _load()
    idx = None
    for i in range(len(exchanges) - 1, -1, -1):
        if (exchanges[i].get("user") or "").strip() == target:
            idx = i
            break
    if idx is None:
        return False
    _save(exchanges[:idx])
    return True


def _truncate(text, max_len):
    if max_len <= 0 or len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def conversation_messages(
    max_exchanges=None,
    char_budget=None,
    recap_exchanges=None,
    recap_budget=None,
):
    """Prior conversation: a compressed recap of older turns, then recent
    turns as real user/assistant messages (oldest first)."""
    recent_n = CONTEXT_EXCHANGES if max_exchanges is None else max_exchanges
    recent_budget = CONTEXT_CHAR_BUDGET if char_budget is None else char_budget
    recap_n = RECAP_EXCHANGES if recap_exchanges is None else recap_exchanges
    recap_lim = RECAP_CHAR_BUDGET if recap_budget is None else recap_budget

    exchanges = _load()
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
        line = f"- User: {user_text} → You: {assistant_text}"
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


def recent_messages(max_exchanges=None, char_budget=None):
    """Back-compat wrapper used by older callers."""
    return conversation_messages(max_exchanges=max_exchanges, char_budget=char_budget)


def recent_context(char_budget=None, max_exchanges=None):
    """A compact text block recapping the last few exchanges, meant to be
    folded straight into the system prompt. Returns "" when there's no
    history yet, so callers can skip it with a plain truthiness check.
    """
    budget = CONTEXT_CHAR_BUDGET if char_budget is None else char_budget
    limit = CONTEXT_EXCHANGES if max_exchanges is None else max_exchanges
    exchanges = _load()[-limit:]
    if not exchanges:
        return ""

    lines = ["For continuity, a quick recap of your recent conversation with the user:"]
    used = 0
    included = 0
    for ex in exchanges:
        line = f"- They asked: {ex.get('user', '')!r} \u2014 you replied: {ex.get('jarvis', '')!r}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
        included += 1

    if included == 0:
        return ""

    lines.append(
        "Only use this if it's actually relevant to the new message below \u2014 don't force a "
        "callback to old context that has nothing to do with what's being asked now."
    )
    return "\n".join(lines)
