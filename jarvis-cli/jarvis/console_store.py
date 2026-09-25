"""Master plan Part E.4 — the console store.

A dedicated, append-only, per-line record of what happened during a turn:
provider attempts and failovers, tool calls and their (capped) results,
narration alongside a tool call, token counts, errors and status lines.
One file per conversation, under ``~/.jarvis/console/<conv_id>.jsonl`` —
same storage shape as conversations.py and logs.py on purpose (JSON Lines,
append-only, one file per conversation), but a SEPARATE file from both:

- separate from conversations.py's ``<conv_id>.json`` (the final exchange
  record — text + extras) because that is only ever written once a turn
  resolves, and the whole point of this store is to survive a turn that
  never resolves;
- separate from logs.py's ``<conv_id>.jsonl`` (raw request/response/
  tool_call traffic for the Logs viewer) because clearing the Logs viewer
  must never erase console history, and vice versa (E.3 #5.5, E.4 point 7).

Written to from two very different places, which is why the public API
has two layers:

  - The stateless layer (`append`, `read`, `clear_marker`) takes conv_id
    (and, for `append`, a turn id) explicitly on every call. This is what
    a one-shot CLI invocation uses — `console-append-run` (server.js's
    direct/live command runs, one short-lived `python -m jarvis` process
    per batch) and `console-read`/`console-clear` (the replay API) all
    go through this layer, since there is no "current ask" for those to
    piggyback on.

  - The active-context layer (`begin_turn`, `log`, `end_turn`,
    `active_turn`) is for ai_client.ask() — server.js spawns one
    `python -m jarvis` child per ask (see cli.py/server.js's own
    comments), so exactly one turn is ever in flight in a given process.
    `begin_turn()` mints a turn id and remembers it (plus conv_id) in a
    module-level variable for the rest of that process's life; `log()`
    then needs only a kind and some text, and `end_turn()`/`abandon()`
    read the remembered state back. ai_client.py's own signal handler
    (`abandon_pending_turn`, fired from a dying process) relies on this
    being plain in-process state, not something that needs a lock or a
    round-trip.

Sequencing (E.4's `seq`): rather than a shared counter file two
languages (this module and server.js) would both need to open, lock and
increment correctly, `seq` is derived from wall-clock milliseconds, bumped
by at least 1 on every write within this process so two lines can never
tie even when written in the same millisecond. This is an approximation,
not a linearizable counter — see `_next_seq`'s docstring — but it is
enough for what `seq` is actually used for here: a replay cursor
(`since_seq`) and a per-conversation ordering, never a count of anything.

Capping (E.4 point 1): each line's `text` is capped at MAX_LINE_CHARS with
a visible "... N chars omitted" marker — never a silent empty result. Each
conversation's file is capped at MAX_FILE_BYTES with rotation of the
OLDEST lines once it's exceeded by a margin, leaving a visible "earlier
output trimmed" status line at the front of what's kept (E.4 point 1's
"visible marker", same principle logs.py's own MAX_LINE_CHARS truncation
was supposed to give but didn't, per E.3 #5.2).

Back-compat (E.4 point 6, E.6 step 7): `read_legacy_command_run` adapts
the OLD `command_run` entries logs.py already holds (see cli.py's
`logs-append-run` handler, kept for exactly this) into this store's line
shape, so a conversation that predates this feature still replays
something instead of nothing. New writes never go through logs.py.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import conversations

JARVIS_DIR = Path.home() / ".jarvis"
CONSOLE_DIR = JARVIS_DIR / "console"
ENCODING = "utf-8"

# Per-line cap. Generous enough to keep a useful chunk of a tool result or
# a stack trace, small enough that one runaway line can't make a line
# un-parseable the way logs.py's whole-entry MAX_LINE_CHARS could (E.3 #5.2:
# a >20000-char JSON entry there fails json.loads and is replaced whole).
# Capping the TEXT field before json.dumps means the line as a whole is
# always valid JSON, however big the original text was.
MAX_LINE_CHARS = 4000

# Size cap per conversation file, checked (cheaply, via os.stat) after
# every append. Once exceeded, _trim() keeps only the newest KEEP_LINES
# lines and prepends one visible "earlier output trimmed" status line —
# never a silent gap.
MAX_FILE_BYTES = 2_000_000
KEEP_LINES_ON_TRIM = 3000

# The known kinds (E.5). Not a hard enum — an unrecognized kind is still
# written and still replayed, just shown as "other" by a filter UI that
# doesn't know it — but this is the vocabulary the writers in this
# codebase actually use, and what the filter UI's checkboxes are built
# from. Two additions beyond E.5's own proposed list, both writers added
# in this patch: "command" (the user's own text — E.5 proposed it for the
# Live Feed's "$ cmd" line; reused here for an ask's opening line too, so
# a replay can show what was actually asked) and "narration" (Part A §5's
# interim text alongside a tool call — narration, not the model's internal
# reasoning, so it isn't folded into "thinking").
KINDS = (
    "stdout", "stderr", "command", "tool-call", "tool-result", "provider",
    "tokens", "thinking", "narration", "notification", "status", "error",
)

_CLEAR_TEXT = "cleared"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _path(conv_id):
    return CONSOLE_DIR / f"{conv_id}.jsonl"


def _clip(text):
    text = "" if text is None else str(text)
    if len(text) <= MAX_LINE_CHARS:
        return text
    omitted = len(text) - MAX_LINE_CHARS
    return text[:MAX_LINE_CHARS] + f"...({omitted} chars omitted)"


# Process-local, monotonic-within-this-process clock for `seq`/turn ids.
# See the module docstring's "Sequencing" section for why this is a
# deliberate approximation rather than a real shared counter.
_last_tick = [0]


def _next_tick():
    now_ms = int(time.time() * 1000)
    tick = now_ms if now_ms > _last_tick[0] else _last_tick[0] + 1
    _last_tick[0] = tick
    return tick


# ---------------------------------------------------------------------------
# Stateless layer
# ---------------------------------------------------------------------------

def append(conv_id, kind, text, *, surface="ask", turn=None, tool=None,
           provider=None, stream=None):
    """Append one event. Never raises \u2014 a console-logging failure must
    never break the ask/run it's trying to record (same contract as
    logs.log). Returns the written line's seq, or None if nothing was
    written (bad conv_id, or an OSError along the way).
    """
    if not conversations.is_valid_id(conv_id):
        return None
    line = {
        "seq": _next_tick(),
        "ts": _now_iso(),
        "turn": turn,
        "surface": surface,
        "kind": kind,
        "tool": tool,
        "provider": provider,
        "stream": stream,
        "text": _clip(text),
    }
    try:
        CONSOLE_DIR.mkdir(parents=True, exist_ok=True)
        path = _path(conv_id)
        with path.open("a", encoding=ENCODING) as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                _trim(conv_id)
        except OSError:
            pass
    except OSError as e:
        print(f"Warning: couldn't write console line for {conv_id}: {e}", file=sys.stderr)
        return None
    return line["seq"]


def _read_raw_lines(conv_id):
    path = _path(conv_id)
    if not path.exists():
        return []
    out = []
    try:
        with path.open("r", encoding=ENCODING) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    # A half-written last line (crash mid-append) is
                    # dropped, never surfaced as a fatal error \u2014 the
                    # whole point of write-ahead-per-line is that a hard
                    # kill loses at most the last line, not the file.
                    continue
    except OSError:
        return []
    return out


def _trim(conv_id):
    """Keep only the newest KEEP_LINES_ON_TRIM lines, with a visible
    marker in front of them so a reload never silently looks shorter than
    it should without saying why (E.4 point 1)."""
    lines = _read_raw_lines(conv_id)
    if len(lines) <= KEEP_LINES_ON_TRIM:
        return
    dropped = len(lines) - KEEP_LINES_ON_TRIM
    kept = lines[-KEEP_LINES_ON_TRIM:]
    marker = {
        "seq": _next_tick(), "ts": _now_iso(), "turn": None, "surface": "live",
        "kind": "status", "tool": None, "provider": None, "stream": None,
        "text": f"earlier output trimmed \u2014 {dropped} line(s) dropped to stay under the size cap",
    }
    path = _path(conv_id)
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding=ENCODING) as f:
            f.write(json.dumps(marker, ensure_ascii=False) + "\n")
            for line in kept:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"Warning: couldn't trim console store for {conv_id}: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def clear_marker(conv_id):
    """E.4 point 7 \u2014 Live Feed 'Clear'. Never deletes anything: just
    appends a status line recording the seq at the moment of clearing, so
    a reload of the LIVE surface can filter to `seq > this` and match what
    the click actually did, while the full history is still on disk for
    anyone asking for it explicitly (`after_last_clear=False`).
    """
    seq = append(conv_id, "status", _CLEAR_TEXT, surface="live")
    return seq


def _cleared_through_seq(lines):
    latest = None
    for line in lines:
        if line.get("kind") == "status" and line.get("text") == _CLEAR_TEXT:
            if latest is None or line.get("seq", 0) > latest:
                latest = line.get("seq", 0)
    return latest


def read(conv_id, *, since_seq=0, kinds=None, turn=None, limit=2000,
         after_last_clear=False, surface=None):
    """Replay query behind `GET /api/console/:id` (server.js shells out to
    the `console-read` CLI command, which calls this \u2014 see that
    command's docstring for why it's a CLI command and not a direct file
    read from Node, matching how /api/logs/:id and /api/conversations/:id
    already work).

    `surface`, when given, restricts to "ask" (ai_client.ask()'s own
    turns) or "live" (server.js's direct/live command runs) \u2014 the two
    write into the SAME per-conversation file (one store, not two), but
    the Live Feed panel and the Ask console are different pieces of UI
    that each want only their own half; kind alone can't separate them
    since both surfaces can produce e.g. "stdout"/"error" lines.

    Returns {"lines": [...], "cleared_through_seq": int|None,
    "last_seq": int|None, "legacy": bool}. "legacy" is True when there is
    no store file at all for this conversation \u2014 the caller (cli.py)
    falls back to read_legacy_command_run() in that case so an
    old conversation still shows something (E.6 step 7).
    """
    lines = _read_raw_lines(conv_id)
    if not lines:
        return {"lines": [], "cleared_through_seq": None, "last_seq": None, "legacy": True}

    cleared_through = _cleared_through_seq(lines)
    effective_since = since_seq or 0
    if after_last_clear and cleared_through is not None:
        effective_since = max(effective_since, cleared_through)

    kind_set = set(kinds) if kinds else None
    out = []
    for line in lines:
        if line.get("seq", 0) <= effective_since:
            continue
        if kind_set is not None and line.get("kind") not in kind_set:
            continue
        if turn is not None and line.get("turn") != turn:
            continue
        if surface is not None and line.get("surface") != surface:
            continue
        out.append(line)

    last_seq = lines[-1].get("seq") if lines else None
    truncated = False
    if limit and len(out) > limit:
        out = out[-limit:]
        truncated = True
    return {
        "lines": out, "cleared_through_seq": cleared_through,
        "last_seq": last_seq, "legacy": False, "truncated": truncated,
    }


def read_legacy_command_run(conv_id, limit=200):
    """E.6 step 7 \u2014 back-compat for a conversation that only has the OLD
    `command_run` log.py entries (see cli.py's `logs-append-run`, which
    used to be direct-run persistence's only mechanism \u2014 E.3 table row
    1). Adapts each one into this store's line shape (one 'command' line
    plus one line per stdout/stderr line plus a closing 'status' line) so
    the same replay UI can show both without a special case.

    Deliberately read-only: this never writes anything back to the console
    store, so a legacy conversation stays exactly as legacy every time
    it's replayed \u2014 the fallback path, not a one-time migration that
    could race a real write into the same file.
    """
    from . import logs as logs_mod
    if not conversations.is_valid_id(conv_id) or not logs_mod.has_log(conv_id):
        return []
    entries = logs_mod.read_entries(conv_id, limit=limit)
    out = []
    seq = 0
    for entry in entries:
        if entry.get("direction") != "command_run":
            continue
        data = entry.get("data") or {}
        if not isinstance(data, dict) or data.get("truncated"):
            # E.3 #5.2's exact failure mode: a >20000-char entry landed as
            # {"truncated": true, "preview": ...} with `lines` gone. Show
            # that it happened rather than silently producing nothing.
            seq += 1
            out.append({
                "seq": seq, "ts": entry.get("ts"), "turn": None, "surface": "live",
                "kind": "status", "tool": None, "provider": None, "stream": None,
                "text": "this run's output was too large for the old storage and was lost "
                        "(fixed by the console store \u2014 see Part E)",
            })
            continue
        seq += 1
        out.append({
            "seq": seq, "ts": entry.get("ts"), "turn": None, "surface": "live",
            "kind": "command", "tool": None, "provider": None, "stream": None,
            "text": data.get("cmdline") or "",
        })
        for item in (data.get("lines") or [])[:5000]:
            seq += 1
            out.append({
                "seq": seq, "ts": entry.get("ts"), "turn": None, "surface": "live",
                "kind": "stdout" if item.get("stream") != "err" else "stderr",
                "tool": None, "provider": None, "stream": item.get("stream"),
                "text": _clip(item.get("text")),
            })
        seq += 1
        exit_code = data.get("exit_code")
        signal_name = data.get("signal")
        status_text = (
            f"exit {signal_name}" if signal_name else
            (f"exit {exit_code}" if exit_code is not None else "exit (unknown)")
        )
        out.append({
            "seq": seq, "ts": entry.get("ts"), "turn": None, "surface": "live",
            "kind": "status", "tool": None, "provider": None, "stream": None,
            "text": status_text,
        })
    return out


# ---------------------------------------------------------------------------
# Active-context layer \u2014 for ai_client.ask()'s single in-flight turn
# ---------------------------------------------------------------------------

_ACTIVE = {"conv_id": None, "turn": None, "surface": "ask", "count": 0}


def line_count_for_turn(conv_id, turn):
    """How many lines this store holds for one turn, regardless of surface.

    Exists for conversations.py's `_reclaim_stale_pending()` (K.2.5.2): the
    reclaim runs in a brand-new process (the one that discovers the stale
    pending exchange left by a SIGKILL'd/OOM'd predecessor), so
    `active_turn()`'s module-level state is long gone — it belonged to a
    process that no longer exists. The turn id itself survives, though,
    because begin_exchange() now saves it on the pending exchange record
    (`consoleTurn`) before a single provider is contacted, same as the text
    the record already saves early for exactly this reason. This is what
    lets that turn id be turned back into a real `{"turn", "lines"}`
    consoleRef pointer, matching the shape `end_turn()` already returns for
    the in-process paths, without conversations.py importing this module at
    module level (it already imports conversations.py the other way, for
    `is_valid_id`) — callers import this function lazily instead.

    Returns 0 (never raises) for a missing file, an unknown turn, or an
    invalid conv_id — "no lines to point at" is a normal, harmless outcome
    here (e.g. the process died before writing even the first "command"
    line), not an error.
    """
    if not conversations.is_valid_id(conv_id) or not turn:
        return 0
    return sum(1 for line in _read_raw_lines(conv_id) if line.get("turn") == turn)


def begin_turn(conv_id, surface="ask"):
    """Start a new turn for conv_id and remember it for log()/end_turn()
    for the rest of this process. Returns the turn id (opaque \u2014 callers
    should treat it as a string/int to pass back, never parse it), or
    None if conv_id is invalid (log() then becomes a silent no-op, same
    "carry on without persistence" contract begin_exchange's docstring
    already documents for conversations.py).
    """
    if not conversations.is_valid_id(conv_id):
        _ACTIVE.update(conv_id=None, turn=None, surface=surface, count=0)
        return None
    turn = f"t{_next_tick()}"
    _ACTIVE.update(conv_id=conv_id, turn=turn, surface=surface, count=0)
    return turn


def active_turn():
    """(conv_id, turn) of the in-flight turn, or (None, None). Used by
    ai_client.abandon_pending_turn() \u2014 fired from a signal handler in the
    SAME process, so this plain module-level state is exactly what it
    needs, no locking or IPC involved."""
    return _ACTIVE["conv_id"], _ACTIVE["turn"]


def log(kind, text, *, tool=None, provider=None, stream=None):
    """Append to the currently active turn. No-op (never raises) if
    nothing is active \u2014 e.g. conv_id was invalid, or log() is called
    after end_turn()/clear_active() already ran."""
    conv_id = _ACTIVE["conv_id"]
    if not conv_id:
        return None
    seq = append(conv_id, kind, text, surface=_ACTIVE["surface"],
                 turn=_ACTIVE["turn"], tool=tool, provider=provider, stream=stream)
    if seq is not None:
        _ACTIVE["count"] += 1
    return seq


def end_turn(status_kind, status_text):
    """Log a final status line for the active turn, return a pointer
    {"turn": ..., "lines": N} for the exchange's saved extras (E.4 point 5
    \u2014 "use it as a pointer (turn id + line count) to the store"), and
    clear the active context. Safe to call with nothing active (returns
    None); safe to call twice (the second call is a no-op after the first
    clears the context)."""
    conv_id, turn = active_turn()
    if not conv_id:
        return None
    log(status_kind, status_text)
    pointer = {"turn": turn, "lines": _ACTIVE["count"]}
    clear_active()
    return pointer


def clear_active():
    _ACTIVE.update(conv_id=None, turn=None, surface="ask", count=0)
