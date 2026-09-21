"""Background clipboard watcher — `jarvis clipboard-watch`.

This is the "continuous watch" half of clipboard_tools.py's Part B spec:
"watch the clipboard and notify me when it changes" needs to keep running
after the ask that requested it ends, which a blocking tool call cannot do
(see clipboard_tools.clipboard_wait_for_change for the one-shot version).

WHY THIS IS A BUILT-IN DAEMON, NOT AN AI-CALLABLE daemon.add()
----------------------------------------------------------------
The original sketch for this feature (see the master plan) had the model
call a `clipboard_watch_start` tool that wraps `daemons.add()`. That
directly conflicts with AGENTS.md's invariant and workspace_tools.py's own
documented reasoning: "there is no daemon_add tool" because registering a
daemon stores an argv Jarvis later runs unattended, and the model is the
component most exposed to text written by other people. A tool that lets
the model register *any* daemon under a friendly name is exactly what that
invariant exists to prevent, even if this particular call site only ever
points at this one script.

So `clipboard-watch` is instead registered as a fourth BUILTIN in
daemons.py — same footing as scheduler/discord/instagram: a fixed,
Jarvis-owned argv, pre-populated in the registry rather than added at
runtime. The model can start/stop/inspect it with the daemon_start/
daemon_stop/daemon_status/list_daemons tools it already has (no new tool
code needed for that part), which is exactly the "start, stop, inspect"
half of the invariant the model IS allowed. What the model does not get is
a way to invent a new daemon with an argv of its choosing.

The optional match pattern is plain data (a regex string used only for
`re.search` against clipboard text, never executed or shelled out), so it
lives in its own small config file — same "plain JSON, created-on-first-
use, re-read-every-call" convention as notifier.py's channel config —
editable by hand via `jarvis clipboard-watch-config`, or by the model via
`clipboard_tools.tool_clipboard_watch_set_pattern` (owner decision D7:
yes, the model may set it — see the master plan's decision log). No
confirmation gate on that tool, same footing as the daemon's own
start/stop already have, since the pattern can only ever be matched
against, never executed.
"""

import re
import time

from . import atomic_io
from . import clipboard_tools
from . import notifier

JARVIS_DIR_NAME = ".jarvis"


def _jarvis_dir():
    # Imported lazily / resolved at call time (not module import time) so
    # this matches daemons.py's own Path.home() timing rather than caching
    # a HOME that a test redirected after this module was first imported.
    from pathlib import Path
    return Path.home() / JARVIS_DIR_NAME


def _config_path():
    return _jarvis_dir() / "clipboard_watch_config.json"


DEFAULT_CONFIG = {
    "pattern": None,       # None/"" = notify on every change
    "poll_seconds": 1.0,   # how often the loop checks the clipboard
}


def load_config():
    """Current watch config, re-read every call (cheap, and lets
    `clipboard-watch-config` change behavior without a restart)."""
    cfg = atomic_io.read_json(_config_path(), default=dict(DEFAULT_CONFIG))
    if not isinstance(cfg, dict):
        cfg = dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(**updates):
    cfg = load_config()
    cfg.update({k: v for k, v in updates.items() if v is not None or k in cfg})
    _jarvis_dir().mkdir(parents=True, exist_ok=True)
    atomic_io.write_json(_config_path(), cfg)
    return cfg


def set_pattern(pattern):
    """`pattern` may be "" or None to clear (notify on every change).
    Validated with re.compile so a typo'd regex fails loudly here instead
    of silently never matching inside the background loop."""
    if pattern:
        re.compile(pattern)  # raises re.error on bad input — let it propagate
    cfg = load_config()
    cfg["pattern"] = pattern or None
    _jarvis_dir().mkdir(parents=True, exist_ok=True)
    atomic_io.write_json(_config_path(), cfg)
    return cfg


def _preview(text):
    text = text or ""
    truncated = len(text) > clipboard_tools.CLIPBOARD_MAX_CHARS
    return text[:clipboard_tools.CLIPBOARD_MAX_CHARS], truncated


def run(poll_seconds=None):
    """Entry point for `jarvis clipboard-watch`. Blocks; the daemon
    supervisor (daemons.py) owns starting, stopping and restarting this
    process — see this module's docstring for why there's no separate
    stop-flag handling here, unlike sched_daemon.py's own loop.

    Returns an exit code, matching every other `jarvis <x>-daemon` /
    `jarvis <x>-serve` entry point (see channels/instagram_gateway.py).
    """
    baseline, err = clipboard_tools._get()
    if err is not None:
        # A headless box with no clipboard utility installed, or no
        # display server at all — clipboard_tools._get() already returns a
        # message that names the actual fix (see its own docstring).
        print(f"clipboard-watch: {err}", flush=True)
        return 1
    baseline = baseline or ""

    print("clipboard-watch: running", flush=True)
    while True:
        cfg = load_config()
        try:
            interval = float(poll_seconds if poll_seconds is not None else cfg.get("poll_seconds", 1.0))
        except (TypeError, ValueError):
            interval = 1.0
        interval = max(0.2, min(interval, 10.0))
        time.sleep(interval)

        current, err = clipboard_tools._get()
        if err is not None:
            # Transient read failure (e.g. another process briefly holding
            # the clipboard) — log and keep polling rather than exiting,
            # since exiting would just get the supervisor to restart us
            # into the same state.
            print(f"clipboard-watch: read error: {err}", flush=True)
            continue
        current = current or ""
        if current == baseline:
            continue
        baseline = current

        pattern = cfg.get("pattern")
        if pattern:
            try:
                if not re.search(pattern, current):
                    continue
            except re.error as exc:
                print(f"clipboard-watch: bad pattern {pattern!r}: {exc}", flush=True)
                continue

        preview, truncated = _preview(current)
        notifier.notify(
            "Clipboard changed",
            preview + (" …" if truncated else ""),
            kind="clipboard_watch",
        )
