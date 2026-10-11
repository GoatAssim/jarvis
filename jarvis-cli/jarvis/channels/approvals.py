"""Approval prompts in chat for confirm-gated tools (master plan L.29 / L.42.1).

THE PROBLEM THIS SOLVES
-----------------------
`ai_client` cancels a confirm-gated tool (`send_dm`, `run_shell`, ...) whenever
no `on_confirm_request` is passed, and chat passed none. So "tell Maryem I'll be
late" typed on Discord was always cancelled. This module is the missing
confirmation channel, built to the owner's answers (Q-L42f / Q-L42h):

    Discord    the bot posts the exact action with Approve / Deny buttons
    Instagram  the bot posts the exact action and waits for a whole-message Y or N
    both       60 seconds, then it counts as DENIED; a restart cancels every
               pending request; only the verified owner can answer

RULES THAT MUST HOLD (each one is pinned by tests/test_chat_approvals.py)
-------------------------------------------------------------------------
* Only an ask the OWNER started can raise a prompt. `base.handle_message` builds
  the callback only when the sender is the owner, so a guest's turn still gets
  `on_confirm_request=None` and `run_shell` / `code_agent` / `edit_file` stay
  cancelled from a guest turn (L.29 note 1). Nothing in here can widen that.
* Only the owner's id can answer. The id is stored on the request when it is
  made; `resolve()` compares the answering user against it (the INTERACTION's
  user on Discord, not the thread), and a request answers once.
* The action shown is the action that runs. `ai_client` hands this module the
  very `arguments` it will execute on approval; nothing is re-generated between
  the prompt and the run. If the action cannot be shown IN FULL in one message it
  is not asked at all -- it is denied with a stated cause (never "approve what you
  could not read").
* Fail closed everywhere: a prompt that could not be sent, a timeout, an
  unreadable store, a restart, a late press, an answer from anyone else -> denied.
* The pending store is on disk so a restart cannot leave an approvable ghost:
  `cancel_pending(platform)` empties that platform's rows when its gateway starts,
  and a press or reply is only honoured while the row is still in memory AND
  unsettled. The store holds the tool name, the platform and times only -- never
  the arguments or the message text.
* Instagram text answers are accepted only from the owner's DM, only while a
  request is pending, and only as the WHOLE message (y / yes / n / no). Any other
  message from the owner while one is pending DENIES it (so the ask does not sit
  on the ask lock for 60 seconds) and is then handled as a normal message.

WHAT THIS DOES NOT CHANGE
-------------------------
`tool_safety`, `risk_review()` and the confirm gate inside `_make_tool_executor`
are untouched (AGENTS.md); this only supplies the callback that gate was always
written to call. `send_dm` stays owner-only, rate limited, in
`TOOL_CONFIRM_REQUIRED` and refuses unattended runs.
"""

import re
import secrets
import threading
import time

from .. import atomic_io
from .directory import CHANNELS_DIR

APPROVAL_SECONDS = 60
PENDING_FILE = CHANNELS_DIR / "pending_approvals.json"

APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"

_YES = {"y", "yes"}
_NO = {"n", "no"}
_ANSWER = re.compile(r"^\s*([A-Za-z]+)\s*[.!]?\s*$")

# Longest a single argument value is shown before the action counts as "not
# shown in full". Generous on purpose: a refusal for length is the safe failure,
# but it should be rare.
MAX_VALUE_CHARS = 700

_LOCK = threading.Lock()
_PENDING = {}


class _Pending:
    __slots__ = ("id", "platform", "owner_id", "tool", "accept_text", "created",
                 "expires", "event", "decision", "reason", "on_settled")

    def __init__(self, platform, owner_id, tool, accept_text, seconds):
        self.id = secrets.token_hex(4)
        self.platform = platform
        self.owner_id = str(owner_id)
        self.tool = str(tool)
        self.accept_text = bool(accept_text)
        self.created = time.time()
        self.expires = self.created + seconds
        self.event = threading.Event()
        self.decision = None
        self.reason = ""
        self.on_settled = None


# --- the on-disk store (names and times only) ------------------------------

def _load_rows():
    data = atomic_io.read_json(PENDING_FILE, default=[], expect=list)
    return data if isinstance(data, list) else []


def _save_rows(rows):
    return atomic_io.write_json(PENDING_FILE, rows)


def _store_add(p):
    rows = [r for r in _load_rows() if isinstance(r, dict) and r.get("id") != p.id]
    rows.append({"id": p.id, "platform": p.platform, "tool": p.tool,
                 "created": p.created, "expires": p.expires})
    return _save_rows(rows)


def _store_remove(request_id):
    rows = _load_rows()
    keep = [r for r in rows if not (isinstance(r, dict) and r.get("id") == request_id)]
    if len(keep) != len(rows):
        _save_rows(keep)


def cancel_pending(platform):
    """Gateway start: nothing that was pending survives a restart.

    Settles anything still in memory as denied (a test or an embedded caller may
    have some) and empties this platform's rows on disk. Other platforms' rows
    are left alone -- the Discord daemon and the Instagram server are separate
    processes sharing this file. Returns how many rows were dropped."""
    with _LOCK:
        mine = [p for p in _PENDING.values() if p.platform == platform]
    for p in mine:
        _settle(p, DENIED, "the gateway restarted")
    rows = _load_rows()
    keep = [r for r in rows if not (isinstance(r, dict) and r.get("platform") == platform)]
    if len(keep) != len(rows):
        _save_rows(keep)
    return len(rows) - len(keep)


# --- showing the action ------------------------------------------------------

def _one_line(value):
    if isinstance(value, str):
        return value
    try:
        import json
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def render_action(tool, arguments, risk_note=None, limit=1500):
    """The exact action as text, plus whether ALL of it fit.

    Returns (text, complete). `complete` is False when an argument value is
    longer than MAX_VALUE_CHARS or the whole text would pass `limit`; the caller
    must then refuse rather than ask (an approval is only meaningful for an action
    the owner could read in full). The wording of the footer is the gateway's."""
    complete = True
    lines = [f"Jarvis wants to run: {tool}"]
    args = arguments if isinstance(arguments, dict) else {}
    for key in sorted(args):
        value = _one_line(args[key])
        if len(value) > MAX_VALUE_CHARS:
            complete = False
            value = value[:MAX_VALUE_CHARS] + " ..."
        lines.append(f"  {key}: {value}")
    if not args and arguments not in (None, {}):
        lines.append(f"  arguments: {_one_line(arguments)[:MAX_VALUE_CHARS]}")
    if isinstance(risk_note, dict):
        note = risk_note.get("note") or risk_note.get("summary") or risk_note.get("message")
        if isinstance(note, str) and note.strip():
            lines.append("Review: " + note.strip()[:300])
    elif isinstance(risk_note, str) and risk_note.strip():
        lines.append("Review: " + risk_note.strip()[:300])
    text = "\n".join(lines)
    if len(text) > limit:
        complete = False
        text = text[:limit].rstrip() + " ..."
    return text, complete


# --- asking ---------------------------------------------------------------

def _settle(p, decision, reason=""):
    """Record the outcome once. Returns True when THIS call settled it."""
    with _LOCK:
        if p.decision is not None:
            return False
        p.decision = decision
        p.reason = reason or ""
        _PENDING.pop(p.id, None)
    _store_remove(p.id)
    p.event.set()
    hook = p.on_settled
    if callable(hook):
        try:
            hook(decision, p.reason)
        except Exception:  # noqa: BLE001 -- tidying a message must never change the outcome
            pass
    return True


def ask_owner(platform, owner_id, tool, arguments, risk_note, prompter, accept_text=False,
              limit=1500, seconds=None, log=None):
    """Put one approval prompt to the owner and wait. Returns True only on approval.

    `prompter(request_id, text, seconds)` sends the prompt (Discord: text plus
    buttons; Instagram: text asking for Y or N). It may return a callable
    `settled(decision, reason)` that the gateway uses to tidy the prompt (disable
    the buttons); raising means the prompt could not be sent, which is a denial.
    `log(decision, reason)`, if given, writes the outcome to the transcript.
    Never raises."""
    seconds = float(seconds or APPROVAL_SECONDS)

    def finish(decision, reason):
        if log is not None:
            try:
                log(decision, reason)
            except Exception:  # noqa: BLE001
                pass
        return decision == APPROVED

    try:
        text, complete = render_action(tool, arguments, risk_note, limit=limit)
        if not complete:
            return finish(DENIED, "the action is too long to show in full here, so it was not run")
        p = _Pending(platform, owner_id, tool, accept_text, seconds)
        if not _store_add(p):
            return finish(DENIED, "the pending-approval store could not be written")
        with _LOCK:
            _PENDING[p.id] = p
        try:
            handle = prompter(p.id, text, seconds)
        except Exception as exc:  # noqa: BLE001 -- any failure to ask is a "no"
            _settle(p, DENIED, f"the prompt could not be sent ({exc})")
            return finish(DENIED, p.reason)
        if callable(handle):
            p.on_settled = handle
            if p.decision is not None:
                # Settled between sending and registering the hook: tidy it now.
                try:
                    handle(p.decision, p.reason)
                except Exception:  # noqa: BLE001
                    pass
        if not p.event.wait(seconds):
            _settle(p, EXPIRED, f"no answer in {int(seconds)} seconds")
        return finish(p.decision or DENIED, p.reason)
    except Exception as exc:  # noqa: BLE001
        return finish(DENIED, f"approval failed ({exc})")


# --- answering --------------------------------------------------------------

def resolve(request_id, user_id, approve, platform=None):
    """Apply one answer. Returns (ok, message): ok is True only when the request
    existed, was still open, and `user_id` is the owner it was raised for."""
    with _LOCK:
        p = _PENDING.get(str(request_id))
    if p is None:
        return False, "That request is no longer open (it expired, was answered, or Jarvis restarted)."
    if platform is not None and p.platform != platform:
        return False, "That request belongs to another platform."
    if str(user_id) != p.owner_id:
        return False, "Only the owner can answer this."
    if time.time() > p.expires:
        _settle(p, EXPIRED, "no answer in time")
        return False, "That request expired, so it was denied."
    if not _settle(p, APPROVED if approve else DENIED, "answered by the owner"):
        return False, "That request was already settled."
    return True, "Approved." if approve else "Denied."


def _pending_text_for(platform, user_id):
    with _LOCK:
        return [p for p in _PENDING.values()
                if p.platform == platform and p.owner_id == str(user_id) and p.accept_text]


def parse_answer(text):
    """'y' / 'yes' -> True, 'n' / 'no' -> False, anything else -> None.
    The message must be only that word (an optional full stop or '!' allowed)."""
    match = _ANSWER.match(str(text or ""))
    if not match:
        return None
    word = match.group(1).lower()
    if word in _YES:
        return True
    if word in _NO:
        return False
    return None


def answer_text(platform, user_id, text, is_dm=True, is_owner=False):
    """An incoming chat message, checked against pending TEXT approvals (Instagram).

    Returns the reply to send, or None when the message is ordinary and should go
    on to the model. Only the owner, only from a DM, only while a request is
    pending: a "y" in a normal conversation never approves anything. Any other
    message from the owner while one is pending denies it and returns None."""
    if not is_owner:
        return None
    pending = _pending_text_for(platform, user_id)
    if not pending:
        return None
    verdict = parse_answer(text) if is_dm else None
    if verdict is None:
        for p in pending:
            _settle(p, DENIED, "the owner sent a different message")
        return None
    reply = ""
    for p in pending:
        ok, message = resolve(p.id, user_id, verdict, platform=platform)
        reply = message if ok or not reply else reply
    return reply or None


def pending_count(platform=None):
    with _LOCK:
        return sum(1 for p in _PENDING.values() if platform in (None, p.platform))
