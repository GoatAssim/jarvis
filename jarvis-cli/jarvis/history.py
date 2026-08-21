"""Rolling conversation memory for 'jarvis <text>'.

Every invocation of jarvis is a brand-new process \u2014 there's no long-running
server holding conversation state in memory (see cli.py's own notes on this
philosophy). This file on disk is what gives the AI continuity between one
"jarvis ..." call and the next: each exchange is appended here, and a
compact recap of the recent ones is folded into the system prompt on every
new call: each exchange is appended here, and the last few turns are sent
back to the model as real user/assistant messages on the next call (see
ai_client.py) so follow-ups and clarifying answers stay in context.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
HISTORY_FILE = JARVIS_DIR / "conversation_history.json"
ENCODING = "utf-8"

MAX_STORED_EXCHANGES = 40      # how many exchanges live on disk
CONTEXT_EXCHANGES = 6          # how many prior turns to send as real chat messages
CONTEXT_CHAR_BUDGET = 2500     # rough cap on prior-turn text (full providers)


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


def recent_messages(max_exchanges=None, char_budget=None):
    """Prior conversation as real user/assistant messages (oldest first).

    ai_client.py inserts these between the system prompt and the current
    user message so follow-ups like answering clarifying questions work."""
    limit = CONTEXT_EXCHANGES if max_exchanges is None else max_exchanges
    budget = CONTEXT_CHAR_BUDGET if char_budget is None else char_budget
    exchanges = _load()[-limit:]
    if not exchanges or budget <= 0:
        return []

    messages = []
    used = 0
    for ex in exchanges:
        user_text = (ex.get("user") or "").strip()
        assistant_text = (ex.get("jarvis") or "").strip()
        if not user_text or not assistant_text:
            continue

        pair_len = len(user_text) + len(assistant_text)
        if used + pair_len > budget:
            if messages:
                break
            remaining = budget
            if remaining < 80:
                break
            if len(user_text) > remaining // 2:
                user_text = _truncate(user_text, remaining // 2)
            remaining -= len(user_text)
            assistant_text = _truncate(assistant_text, max(remaining, 40))

        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})
        used += len(user_text) + len(assistant_text)

    return messages


def recent_context(char_budget=None, max_exchanges=None):
    """A compact text block recapping the last few exchanges, meant to be
    folded straight into the system prompt. Returns "" when there's no
    history yet, so callers can skip it with a plain truthiness check.

    char_budget / max_exchanges override the module defaults — ai_client.py
    passes smaller values for rate-sensitive providers (e.g. Groq)."""
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
