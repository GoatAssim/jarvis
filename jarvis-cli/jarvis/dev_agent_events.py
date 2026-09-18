"""Progress-event emission for dev_agent (and, generically, any future
long-running single-tool-call agent that wants live sub-step visibility
without re-entering the model's tool-calling loop).

Mirrors present_tools.py's JARVIS_MEDIA convention exactly, just with a
JSON payload instead of fixed positional fields — the per-event payload
for dev_agent is too structurally varied (a file_written event wants a
path + byte count + preview; a run_result event wants an exit code +
stdout/stderr tail; a fix_attempt event wants an attempt number + a
classified error + which file changed) to force into a fixed number of
positional tab-fields the way present_file's line does. This keeps the
same two-field envelope prefix (`JARVIS_MEDIA\tdev_agent`) that
addAskPromptTrace's existing `line.split("\t")` / `parts[1] === "..."`
dispatch pattern already expects, and puts the entire event as one
JSON blob in the third field.

Emits to stderr, flush=True, same as every other JARVIS_MEDIA producer,
so web/server.js's line-buffered stdio forwarding (spawnAndStream's
onErr) delivers each event to the browser the instant it's printed —
this is what makes it "real time" rather than a batch dumped at
tool-return time.

CLI mode has no subprocess boundary to intercept the way server.js
intercepts a spawned child's pipes for the web UI (dev_agent runs
in-process, inside the same interpreter as cli.py itself), so a plain
stderr print alone wouldn't reach cli.py's own pretty-printing path.
set_hook()/emit() below is the seam that lets cli.py additionally see
every event as it's produced, without dev_agent.py needing to know or
care whether anything is listening.
"""

import json
import sys

_cli_hook = None  # optional Callable[[dict], None], registered by cli.py


def set_hook(fn):
    """Register a callback invoked with the same event dict emit() just
    printed, so a plain-terminal session (cli.py) can pretty-print
    dev_agent's progress without a subprocess stdio boundary to
    intercept. Pass None to clear it. Never raises on a bad callback —
    a broken hook must not take down the agent loop over a logging call.
    """
    global _cli_hook
    _cli_hook = fn


def emit(job_id, seq, phase, status, **fields):
    """Print one JARVIS_MEDIA dev_agent event line. Never raises — a
    malformed/unserializable field must never take down the agent loop
    over a logging call. Returns the event dict actually emitted (or the
    fallback error-shaped one) so callers building a `steps` list for the
    final persisted result can append the exact same object the live
    stream and the CLI hook both saw — one shared representation, not
    three independently-maintained copies that can drift apart.
    """
    event = {"job_id": job_id, "seq": seq, "phase": phase, "status": status, **fields}
    try:
        line = "JARVIS_MEDIA\tdev_agent\t" + json.dumps(event, default=str, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — see docstring; this must not raise
        # Deliberately Exception and not (TypeError, ValueError). `default=str`
        # means json.dumps falls back to str() on anything it can't encode, so
        # the failure that actually reaches here is str()/__repr__() ITSELF
        # raising — and it can raise anything at all. A RuntimeError out of a
        # field's __repr__ used to escape emit() entirely and abort the
        # dev_agent loop over a logging call, which is exactly what the
        # docstring above promises cannot happen.
        try:
            detail = str(e)
        except Exception:  # noqa: BLE001 — the exception's own __str__ can raise too
            detail = type(e).__name__
        event = {"job_id": job_id, "seq": seq, "phase": phase, "status": status,
                 "error": f"event serialization failed: {detail}"}
        try:
            line = "JARVIS_MEDIA\tdev_agent\t" + json.dumps(event)
        except Exception:  # noqa: BLE001
            # Even the fallback can fail if job_id/phase/status are themselves
            # exotic objects, since this dumps() has no default=. Last resort:
            # a valid three-field envelope with everything coerced.
            event = {"job_id": repr(job_id)[:120], "seq": -1, "phase": "unknown",
                     "status": "error", "error": "event serialization failed"}
            line = "JARVIS_MEDIA\tdev_agent\t" + json.dumps(event)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    if _cli_hook is not None:
        try:
            _cli_hook(event)
        except Exception:
            pass
    return event
