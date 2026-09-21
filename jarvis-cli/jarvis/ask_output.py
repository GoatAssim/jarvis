"""Shared helpers for cleaning up captured `jarvis ask`/`jarvis <command>`
stdout before it's shown to a person somewhere that isn't the live
CLI/web session that already gets the clean version for free (master plan
Part D.2: "notifications listing scheduled jobs showed raw JARVIS_USAGE
{...} JSON under each entry").

Two separate problems, two functions, one shared module so every call
site uses the same logic instead of copy-pasting it (or, worse, one of
them forgetting to):

  * strip_protocol_lines() drops cli.py's machine-readable marker lines
    (currently JARVIS_USAGE and JARVIS_CONFIRM_REQUEST) from captured
    stdout. A LIVE session never shows these to a person in the first
    place — a terminal prints the human-readable line instead and the
    marker only exists for server.js to peel off before anything reaches
    the browser (see cli.py's own comments next to each `print`). Only a
    caller that captures raw stdout for later — a scheduled job with no
    live listener, a task step's checkpointed result — has to strip them
    by hand. This used to live only in scheduler.py as a module-private
    `_strip_protocol_lines`; it's here now so task_runner.py (and
    anywhere else with the exact same problem) can use the same
    implementation. scheduler.py keeps its old private name as a thin
    alias so nothing that already imports it breaks.

  * summarize() is the actual D.2 ask: a short summary for a notification
    body (or a task's headline result), with the full cleaned text kept
    alongside it for an "expand" view — rather than dumping the whole
    reply into the place a person glances at first.

Deliberately NOT here: digest.py's grouped, scheduled summary of several
LOW-priority notifications batched together. That's a different feature
(batching routine notifications so they don't each interrupt on their
own) solving a different problem from this module's (one notification's
body shouldn't itself be a wall of raw output) — see the master plan's
Part D.2 cross-check note. The two can layer: digest.py can group
notifications whose `message` this module already cleaned.
"""

# cli.py prints these as the first token of a line when it wants a
# WebSocket-listening web session to pick up a structured event; a
# session that isn't listening (a scheduled job's captured subprocess
# stdout, a task step's stdout) just sees them as literal lines of text.
PROTOCOL_LINE_PREFIXES = ("JARVIS_USAGE ", "JARVIS_CONFIRM_REQUEST ")

DEFAULT_SUMMARY_CHARS = 200


def strip_protocol_lines(text):
    """Drop cli.py's machine-readable marker lines from captured stdout.
    Never raises; returns falsy input unchanged."""
    if not text:
        return text
    kept = [ln for ln in text.split("\n") if not ln.startswith(PROTOCOL_LINE_PREFIXES)]
    return "\n".join(kept).strip()


def summarize(text, max_chars=DEFAULT_SUMMARY_CHARS):
    """Return {"summary": str, "truncated": bool, "full": str} for `text`.

    `text` is run through strip_protocol_lines() first, so a caller can
    pass raw captured stdout straight in without stripping it themselves.
    "Summary" is the reply's first non-blank line, capped at `max_chars`
    (truncated with an ellipsis if that line alone is longer); `truncated`
    is True whenever the summary doesn't already show everything there is
    — either because the first line itself was cut, or because there was
    more text after it — so a caller knows whether an "expand" link is
    worth offering. `full` is the cleaned text, for that expand view.
    """
    cleaned = strip_protocol_lines(text or "") or ""
    if not cleaned:
        return {"summary": "", "truncated": False, "full": cleaned}
    lines = cleaned.splitlines()
    first_line = next((ln.strip() for ln in lines if ln.strip()), "")
    more_after_first_line = any(ln.strip() for ln in lines[1:]) if len(lines) > 1 else False
    if len(first_line) <= max_chars:
        summary = first_line
        truncated = more_after_first_line
    else:
        summary = first_line[:max_chars].rstrip() + "…"
        truncated = True
    return {"summary": summary, "truncated": truncated, "full": cleaned}
