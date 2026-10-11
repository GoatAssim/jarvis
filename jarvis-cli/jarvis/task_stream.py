"""Master plan K.3.2.4 -- the word-by-word tier of the subagent live view.

WHY A FILE
----------
A subagent step is its own `jarvis ask` subprocess (task_runner._spawn_ask),
and the supervisor captures that process's stdout with a blocking
`subprocess.run`, so nothing it prints can reach the web UI until the step
ends. The main Ask panel gets its live tokens from `JARVIS_STREAM {json}`
marker lines that web/server.js peels off the child's stdout; a subagent has
no such reader. Rather than turn the capture into a second streaming
channel, the child writes the SAME marker lines (stream_markers.py's
StreamMarkerSink, unchanged) to a per-task file, and the web server reads
that file by byte offset (GET /api/subagents/:id/stream).

    ~/.jarvis/tasks/streams/<task id>.jsonl      one `JARVIS_STREAM {json}` per line

Wire shape = stream_markers.py's, plus two bracketing events this module
writes itself (the child never does):

    JARVIS_STREAM {"k":"step_start","step":"plan"|<1-based int>,"ts":"..."}
    JARVIS_STREAM {"k":"step_end","ok":true|false}

The file is APPEND-ONLY while a step runs (only one writer at a time: the
supervisor writes step_start, then the child writes, then the supervisor
writes step_end). It is bounded: a file over MAX_BYTES is restarted at the
next step_start, and files older than KEEP_SECONDS are deleted. Readers must
treat a file that got SHORTER than their offset as a restart.

Everything here is best effort and never raises: a stream file is cosmetic,
so a read-only disk or a bad path must cost the user a live view, never a
task step.

Escape hatch: JARVIS_SUBAGENT_STREAM=0 keeps subagent steps on the blocking
transport exactly as before this feature existed.
"""

import json
import os
import time
from datetime import datetime, timezone

from . import tasks
from .stream_markers import MARKER

ENV_FILE = "JARVIS_TASK_STREAM_FILE"   # set on the child by task_runner._spawn_ask
ENV_OFF = "JARVIS_SUBAGENT_STREAM"     # "0" disables the whole feature

MAX_BYTES = 256 * 1024
KEEP_SECONDS = 3 * 24 * 3600


def enabled():
    return os.environ.get(ENV_OFF) != "0"


def stream_dir():
    return tasks.TASKS_DIR / "streams"


def path_for(task_id):
    """The stream file for a task, or None for an id tasks.py would refuse
    (so a malformed id can never turn into a path)."""
    if not tasks.is_valid_id(task_id):
        return None
    return stream_dir() / ("%s.jsonl" % task_id)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _append(path, payload, mode="a"):
    with open(path, mode, encoding="utf-8", newline="\n") as fh:
        fh.write(MARKER + json.dumps(payload, ensure_ascii=False) + "\n")


def _prune(directory):
    cutoff = time.time() - KEEP_SECONDS
    try:
        for entry in directory.glob("t_*.jsonl"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


def begin_step(task, step):
    """Open a step's section of the task's stream file. `step` is "plan" or
    the 1-based step number. Returns the file path to hand the child
    (JARVIS_TASK_STREAM_FILE), or "" when streaming is off / not possible."""
    try:
        if not enabled() or not isinstance(task, dict):
            return ""
        path = path_for(task.get("id"))
        if path is None:
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a"
        try:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                mode = "w"   # restart; readers see the file shrink and start over
        except OSError:
            pass
        _append(path, {"k": "step_start", "step": step, "ts": _now()}, mode)
        _prune(path.parent)
        return str(path)
    except Exception:  # noqa: BLE001 -- cosmetic; never costs a step
        return ""


def end_step(path, ok):
    """Close the section begin_step() opened. Safe with an empty path."""
    if not path:
        return
    try:
        _append(path, {"k": "step_end", "ok": bool(ok)})
    except Exception:  # noqa: BLE001
        pass
