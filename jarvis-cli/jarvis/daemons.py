"""One place to see, start, stop, watch and script every long-running
Jarvis process.

WHAT WAS WRONG BEFORE
---------------------
Jarvis had three daemons — the scheduler (`sched_daemon.py`), the Discord
gateway and the Instagram webhook — and no shared anything. Each owned its
own PID file, each had its own ad-hoc `--status`/`--stop` spelling or none
at all, none of them captured their own console output anywhere you could
read it later, and "is the Discord bot actually up?" was answered by
grepping `ps`. There was also no way to run anything else under the same
roof, so a user's own little server sat outside the system entirely.

This module is the missing layer. It owns:

  * a registry of daemons (three built-ins + anything the user adds),
  * starting/stopping them and reporting real status,
  * capturing their console output to rotating files you can tail or
    search (see log_files.py),
  * feeding stdin to the ones that accept it,
  * a next-start time so a daemon can be brought up later rather than now.

THE SUPERVISOR TRICK
--------------------
`jarvis` is a brand-new OS process on every invocation (see history.py), so
there is nobody around to hold a child's pipes. That matters most for
stdin: you cannot write to another process's stdin from a third process.

So starting a daemon is two processes, not one:

    jarvis daemon-start web          (returns immediately)
        └─ spawns, detached:
           jarvis daemon-run web     (the SUPERVISOR — long-lived)
               └─ spawns the actual child, owns its pipes,
                  pumps stdout/stderr into console.log,
                  drains stdin.queue into the child's stdin,
                  and writes status.json as it goes

Everything the rest of the system needs is therefore a file on disk:
status.json to read state, console.log to read output, stdin.queue to
write input, stop.flag to ask for shutdown. No IPC, nothing held in
memory, and every one of those survives the CLI process that created it.

WHY A STDIN *QUEUE* FILE AND NOT A FIFO
---------------------------------------
A named pipe would be the obvious unix answer and does not exist in a
usable form on Windows, which is this project's primary platform. An
append-only queue file drained by the supervisor works identically on
both, is trivially inspectable when something goes wrong, and degrades
safely: if the supervisor dies, the queued input just sits there instead
of blocking a writer forever.

BUILT-INS ARE NOT SPECIAL-CASED
-------------------------------
The three existing daemons are registry entries like any other — they just
ship with the registry pre-populated and their argv points back at
`jarvis`. That is deliberate: it means the "custom daemon" path is the
*same* path the built-ins use and therefore cannot rot from disuse.
"""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import atomic_io, categories as _categories

JARVIS_DIR = Path.home() / ".jarvis"
DAEMON_DIR = JARVIS_DIR / "daemons"
REGISTRY_FILE = JARVIS_DIR / "daemons.json"
ENCODING = "utf-8"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)

# Console log rotation. A gateway left up for a week will happily produce
# tens of MB, and the whole point of capturing output is being able to read
# it — an unbounded file is one you end up deleting instead.
MAX_CONSOLE_BYTES = 2 * 1024 * 1024
MAX_CONSOLE_BACKUPS = 5

# How often the supervisor checks the stdin queue and the stop flag. Short
# enough to feel immediate when typing into the console, long enough that
# an idle supervisor is invisible in a process monitor.
POLL_SECONDS = 0.4

# A child that exits this fast after start almost certainly failed to boot
# (bad token, port in use, missing dependency) rather than "finished".
FAST_EXIT_SECONDS = 3

# Restart policies. "on-failure" is the useful default for a real service
# (a crash is worth recovering from; a clean exit means it finished on
# purpose and respawning it would be a loop), but the registry default is
# "never" so a daemon someone adds behaves exactly as they typed it until
# they ask for more.
RESTART_NEVER = "never"
RESTART_ON_FAILURE = "on-failure"
RESTART_ALWAYS = "always"
RESTART_POLICIES = (RESTART_NEVER, RESTART_ON_FAILURE, RESTART_ALWAYS)

DEFAULT_RESTART_DELAY = 5
DEFAULT_MAX_RESTARTS = 5
DEFAULT_STOP_TIMEOUT = 10
# Restart counting is windowed, not lifetime: a service that has been up for
# a week and then crashes once should get its retries back, otherwise
# max_restarts becomes "this daemon may crash five times ever".
RESTART_WINDOW_SECONDS = 600

STOP_SIGNALS = {
    "TERM": getattr(signal, "SIGTERM", 15),
    "INT": getattr(signal, "SIGINT", 2),
    "KILL": getattr(signal, "SIGKILL", 9),
}

STATUS_STOPPED = "stopped"
STATUS_RUNNING = "running"
STATUS_STARTING = "starting"
STATUS_CRASHED = "crashed"
STATUS_SCHEDULED = "scheduled"
STATUS_RESTARTING = "restarting"

# H.1.1: how long a daemon is allowed to sit in STATUS_STARTING, with its
# supervisor process still alive, before status() gives up waiting and calls
# it crashed. Generous on purpose — this only bounds a supervisor that's
# alive but has gone silent; a supervisor that has already exited is caught
# immediately below regardless of this window.
STARTING_GRACE_SECONDS = 10

# The built-in three. `argv` is resolved through _jarvis_argv() at spawn
# time when the first element is the JARVIS token, so a frozen build, a
# venv and a `python -m jarvis` checkout all work without the registry
# knowing which it is.
JARVIS_TOKEN = "@jarvis"

BUILTINS = {
    "scheduler": {
        "id": "scheduler",
        "name": "Scheduler",
        "builtin": True,
        "argv": [JARVIS_TOKEN, "sched-daemon"],
        "description": "Fires reminders, scheduled tasks and watches on time.",
        "categories": ["core"],
        "supports_stdin": False,
        "enabled": True,
    },
    "discord": {
        "id": "discord",
        "name": "Discord gateway",
        "builtin": True,
        "argv": [JARVIS_TOKEN, "discord-daemon"],
        "description": "Holds the Discord WebSocket open and answers messages.",
        "categories": ["chat"],
        "supports_stdin": False,
        "enabled": True,
    },
    "instagram": {
        "id": "instagram",
        "name": "Instagram webhook",
        "builtin": True,
        "argv": [JARVIS_TOKEN, "instagram-serve"],
        "description": "Receives Instagram DMs over the Meta webhook.",
        "categories": ["chat"],
        "supports_stdin": False,
        "enabled": True,
    },
    "clipboard-watch": {
        "id": "clipboard-watch",
        "name": "Clipboard watch",
        "builtin": True,
        "argv": [JARVIS_TOKEN, "clipboard-watch"],
        "description": (
            "Notifies when the clipboard changes (optionally filtered by a "
            "regex pattern set via 'jarvis clipboard-watch-config'). Off by "
            "default — start it with daemon_start/daemon-start when wanted."
        ),
        "categories": ["desktop"],
        "supports_stdin": False,
        "enabled": True,
    },
    "browser": {
        "id": "browser",
        "name": "Browser (warm)",
        "builtin": True,
        "argv": [JARVIS_TOKEN, "browser-daemon"],
        "description": (
            "Holds a Playwright browser session open across asks instead of "
            "one per ask (master plan Part C, v2). Closes the browser after "
            "'browser_daemon_idle_seconds' (config, default 600s) of no "
            "activity; the daemon keeps listening and reopens it on the next "
            "call. Purely a speed optimization: browser_* tools only use it "
            "when 'browser_warm_daemon' (ai_config.json defaults) is on, and "
            "fall back to their own local session for this ask if it isn't "
            "running or doesn't answer. Off by default — start it with "
            "daemon_start/daemon-start when wanted."
        ),
        "categories": ["desktop"],
        "supports_stdin": False,
        "enabled": True,
    },
}

# Legacy PID files the three built-ins write for themselves. Read (never
# written) so a daemon someone started the old way — `jarvis sched-daemon`
# in a terminal, a Task Scheduler entry, NSSM — still shows as running
# here instead of being reported as stopped next to a process that very
# obviously exists.
_LEGACY_PID_FILES = {
    "scheduler": JARVIS_DIR / "sched_daemon.pid",
    "discord": JARVIS_DIR / "discord_daemon.pid",
    # No entry for instagram: its gateway is an HTTP server that writes no
    # PID file of its own (see channels/instagram_gateway.py), so there is
    # nothing to adopt. It is only ever seen as running when started
    # through this module.
}

_ID_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def normalize_id(value):
    text = str(value or "").strip().lower().replace(" ", "-")
    text = "".join(c for c in text if c in _ID_OK)
    return text[:40]


def _load_registry():
    data = atomic_io.read_json(REGISTRY_FILE, default={}, expect=dict)
    entries = data.get("daemons")
    if not isinstance(entries, dict):
        entries = {}
    merged = {}
    # Built-ins are merged in fresh every read rather than written to the
    # file, so upgrading Jarvis picks up a changed built-in definition
    # without a migration. A stored entry with the same id still wins for
    # the fields a user is allowed to change (enabled, autostart, schedule),
    # which is what makes "disable the Instagram daemon" stick.
    for did, base in BUILTINS.items():
        entry = dict(base)
        stored = entries.get(did)
        if isinstance(stored, dict):
            for field in _EDITABLE:
                if field in _BUILTIN_LOCKED:
                    continue
                if field in stored:
                    entry[field] = stored[field]
        entry["categories"] = _clean_categories(entry.get("categories"))
        merged[did] = entry
    for did, stored in entries.items():
        if did in BUILTINS or not isinstance(stored, dict):
            continue
        entry = dict(stored)
        entry["id"] = did
        entry["builtin"] = False
        # A daemon saved before categories existed has no key at all: that
        # is "no category" (shown as Undefined), not an error and not a
        # migration to run.
        entry["categories"] = _clean_categories(entry.get("categories"))
        merged[did] = entry
    return merged


def _clean_categories(value):
    """Lenient read of a stored `categories`: fix what can be fixed, drop the
    rest. A hand-edited registry must never fail to load over one odd name -
    that would take every daemon down with it (see categories.py)."""
    return _categories.normalize_list(value)[0]


def _save_registry(merged):
    """Persist only what isn't derivable from BUILTINS.

    A built-in is stored as just its user-editable overrides. Writing the
    whole definition would freeze today's argv into the file and silently
    keep using it after an upgrade changed the command.
    """
    out = {}
    for did, entry in merged.items():
        if did in BUILTINS:
            overrides = {k: entry[k] for k in _EDITABLE
                         if k not in _BUILTIN_LOCKED
                         and k in entry and entry[k] != BUILTINS[did].get(k)}
            if overrides:
                out[did] = overrides
        else:
            out[did] = entry
    atomic_io.write_json(REGISTRY_FILE, {"daemons": out})


def list_daemons():
    """Every registered daemon, built-ins first, each with live status."""
    merged = _load_registry()
    items = []
    for did in list(BUILTINS) + sorted(d for d in merged if d not in BUILTINS):
        entry = merged.get(did)
        if entry:
            items.append(describe(entry))
    return items


def get(daemon_id):
    return _load_registry().get(normalize_id(daemon_id))


def _split_command(command, windows=None):
    """Split a command string into argv the way the OS will actually see it.

    On Windows `shlex.split(..., posix=False)` leaves the quote characters
    INSIDE the tokens, so `"C:\\Program Files\\Python\\python.exe" -c "..."`
    became an argv[0] that literally starts with a `"` and Popen failed with
    [WinError 2] (L.27 / test_l27 on Windows). CommandLineToArgvW is what
    every Windows program's own argv is built from, so it is used first;
    if the WinAPI call is unavailable the fallback is shlex(posix=False)
    plus stripping ONE surrounding quote pair per token, which is the same
    result for ordinary command lines. `windows` is overridable so the
    fallback can be exercised from a non-Windows box.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return shlex.split(command, posix=True)
    if not command.strip():
        # CommandLineToArgvW("") returns the CURRENT executable's path.
        return []
    try:
        import ctypes
        argc = ctypes.c_int(0)
        argv_p = ctypes.windll.shell32.CommandLineToArgvW(
            ctypes.c_wchar_p(command), ctypes.byref(argc))
        if argv_p:
            try:
                return [argv_p[i] for i in range(argc.value)]
            finally:
                ctypes.windll.kernel32.LocalFree(argv_p)
    except Exception:  # noqa: BLE001 - no WinAPI here; use the fallback
        pass
    parts = shlex.split(command, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p
            for p in parts]


def add(daemon_id, command, name="", cwd="", env=None, supports_stdin=False,
        description="", autostart=False, shell=False,
        restart=RESTART_NEVER, restart_delay=DEFAULT_RESTART_DELAY,
        max_restarts=DEFAULT_MAX_RESTARTS, stop_signal="TERM",
        stop_timeout=DEFAULT_STOP_TIMEOUT, notes="", categories=None):
    """Register a user-defined daemon.

    `command` may be a list (used verbatim — the safe form) or a string,
    which is split with shlex so `jarvis daemon-add web "node server.js
    --port 3000"` does the obvious thing. shell=True is never used: a
    daemon definition is stored on disk and re-run unattended, and turning
    that into a shell string is how a stored argument becomes a command.
    """
    did = normalize_id(daemon_id)
    if not did:
        return False, "id must contain letters or digits"
    if did in BUILTINS:
        return False, f"'{did}' is a built-in daemon and can't be replaced"
    merged = _load_registry()
    if did in merged:
        return False, f"'{did}' already exists — use daemon-edit"
    # Strict, and before anything else is built: a write that cannot be
    # honoured says so instead of quietly keeping a different set.
    cats, cat_err = _categories.normalize_list(categories, strict=True)
    if cat_err:
        return False, cat_err

    # A SHELL command is stored verbatim, as a single element, and never
    # split. Splitting it and re-joining at spawn time silently destroys the
    # quoting: `python -c "print(1)"` becomes `python -c print(1)`, which the
    # shell then parses as a syntax error. The whole reason to ask for
    # shell=True is that the string means something to a shell, so the string
    # is what has to survive.
    if isinstance(command, str):
        if shell:
            argv = [command.strip()]
        else:
            try:
                argv = _split_command(command)
            except ValueError as exc:
                return False, f"could not parse command: {exc}"
    else:
        argv = [str(c) for c in (command or [])]
    if not argv or not any(str(a).strip() for a in argv):
        return False, "command is required"

    merged[did] = {
        "id": did,
        "name": name or did,
        "builtin": False,
        "argv": argv,
        "cwd": str(cwd or ""),
        "env": dict(env or {}),
        "supports_stdin": bool(supports_stdin),
        "description": description or "",
        # Empty = "Undefined" in the panel (L.11). Not a category in its own
        # right, and not the same thing as `builtin`, which is a lock.
        "categories": cats,
        "enabled": True,
        "autostart": bool(autostart),
        "next_start": None,
        # --- how it runs -------------------------------------------------
        # shell=True is opt-in and stored per daemon rather than being the
        # default, because it changes what the stored `argv` MEANS: with it
        # on, the command is re-interpreted by cmd.exe/sh every start, so a
        # value that was safe as an argv element (a filename with a
        # semicolon in it) becomes two commands. Anyone who needs pipes or
        # redirection genuinely needs it; nobody should get it by accident.
        "shell": bool(shell),
        "restart": restart if restart in RESTART_POLICIES else RESTART_NEVER,
        "restart_delay": max(1, int(restart_delay or DEFAULT_RESTART_DELAY)),
        "max_restarts": max(0, int(max_restarts if max_restarts is not None
                                   else DEFAULT_MAX_RESTARTS)),
        "stop_signal": stop_signal if stop_signal in STOP_SIGNALS else "TERM",
        "stop_timeout": max(1, int(stop_timeout or DEFAULT_STOP_TIMEOUT)),
        "notes": notes or "",
    }
    _save_registry(merged)
    return True, ""


_EDITABLE = ("name", "argv", "cwd", "env", "supports_stdin", "description",
             "enabled", "autostart", "next_start", "notes", "shell",
             "restart", "restart_delay", "max_restarts", "stop_signal",
             "stop_timeout", "categories")

# Fields a built-in refuses to change. All three decide what actually gets
# executed or how: rewriting them would turn "start the scheduler" into
# running something else, which is a privilege escalation wearing a config
# edit's clothes.
_BUILTIN_LOCKED = ("argv", "supports_stdin", "shell")

# Coercions for edit(), so `daemon-edit web --restart-delay 2` stores an int
# and not the string "2" — which would compare fine and then fail at
# time.sleep().
_EDIT_COERCE = {
    "restart_delay": lambda v: max(1, int(v)),
    "max_restarts": lambda v: max(0, int(v)),
    "stop_timeout": lambda v: max(1, int(v)),
    "shell": lambda v: bool(v),
    "enabled": lambda v: bool(v),
    "autostart": lambda v: bool(v),
    "supports_stdin": lambda v: bool(v),
}


def edit(daemon_id, **fields):
    did = normalize_id(daemon_id)
    merged = _load_registry()
    if did not in merged:
        return False, f"no daemon '{did}'"
    entry = merged[did]
    builtin = did in BUILTINS
    # Apply `shell` before anything else: whether a command string gets split
    # depends on it, so processing them in dict order would make the result
    # depend on keyword ordering at the call site.
    ordered = sorted(fields.items(), key=lambda kv: 0 if kv[0] == "shell" else 1)
    for key, value in ordered:
        if value is None:
            continue
        if key not in _EDITABLE:
            return False, f"'{key}' is not editable"
        # A built-in's argv is owned by Jarvis — see _save_registry. Letting
        # it be rewritten would turn "start the scheduler" into "run this
        # arbitrary command", which is a privilege escalation dressed up as
        # a config edit.
        if builtin and key in _BUILTIN_LOCKED:
            return False, f"'{key}' can't be changed on a built-in daemon"
        if key == "argv" and isinstance(value, str):
            # Same shell rule as add(): a shell command is kept verbatim.
            # `fields` is a dict so `shell` may or may not be in this same
            # edit — check the pending value first, then what is stored.
            wants_shell = bool(fields.get("shell", entry.get("shell")))
            if wants_shell:
                value = [value.strip()]
            else:
                try:
                    value = _split_command(value)
                except ValueError as exc:
                    return False, f"could not parse command: {exc}"
        if key == "restart" and value not in RESTART_POLICIES:
            return False, ("restart must be one of: "
                           + ", ".join(RESTART_POLICIES))
        if key == "stop_signal" and value not in STOP_SIGNALS:
            return False, "stop_signal must be one of: " + ", ".join(STOP_SIGNALS)
        if key == "categories":
            # The whole set is replaced, never merged: an editor that shows
            # every chip and lets one be removed has to be able to say "now
            # there are fewer". `[]` is therefore a real value (clear them
            # all); only None - skipped above - means "leave it alone".
            value, cat_err = _categories.normalize_list(value, strict=True)
            if cat_err:
                return False, cat_err
        if key in _EDIT_COERCE:
            try:
                value = _EDIT_COERCE[key](value)
            except (TypeError, ValueError):
                return False, f"'{key}' must be a number"
        entry[key] = value
    merged[did] = entry
    _save_registry(merged)
    return True, ""


def remove(daemon_id):
    did = normalize_id(daemon_id)
    if did in BUILTINS:
        return False, "built-in daemons can't be removed — disable it instead"
    merged = _load_registry()
    if did not in merged:
        return False, f"no daemon '{did}'"
    del merged[did]
    _save_registry(merged)
    return True, ""


# ---------------------------------------------------------------------------
# per-daemon paths
# ---------------------------------------------------------------------------

def daemon_dir(daemon_id):
    path = DAEMON_DIR / normalize_id(daemon_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def console_path(daemon_id):
    return daemon_dir(daemon_id) / "console.log"


def status_path(daemon_id):
    return daemon_dir(daemon_id) / "status.json"


def stdin_queue_path(daemon_id):
    return daemon_dir(daemon_id) / "stdin.queue"


def stop_flag_path(daemon_id):
    return daemon_dir(daemon_id) / "stop.flag"


def console_backups(daemon_id):
    """Rotated console logs, newest first. What the Logs tab lists."""
    base = daemon_dir(daemon_id)
    files = sorted(base.glob("console.log.*"),
                   key=lambda p: p.stat().st_mtime if p.exists() else 0,
                   reverse=True)
    return [str(p) for p in files]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _on_windows():
    # A function rather than a constant so a test can exercise the Windows
    # branch on any host.
    return os.name == "nt"


# Win32 constants for the liveness probe below.
_WIN_QUERY_LIMITED_INFORMATION = 0x1000   # enough for GetExitCodeProcess
_WIN_ACCESS_DENIED = 5                    # ERROR_ACCESS_DENIED
_WIN_STILL_ACTIVE = 259


def _win_kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _win_last_error():
    import ctypes
    getter = getattr(ctypes, "get_last_error", None)   # Windows-only attribute
    return getter() if getter else 0


def _pid_alive_windows(pid):
    """Is this pid a running process? Never signals it.

    H.1.6 (owner-reported, 2026-09-29: the Daemons panel never showed
    anything as running). This used to be `os.kill(pid, 0)` on every
    platform. On POSIX signal 0 is the standard existence probe; on Windows
    it is not — `signal.CTRL_C_EVENT` is 0, so CPython turns `os.kill(pid,
    0)` into GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid). That is not an
    existence check: it targets a console process *group*, so for an
    ordinary pid it fails with an OSError (which the old code read as \"no
    such process\" — every live daemon looked dead) and where it does
    land it delivers a real Ctrl+C to the target. With every pid reading
    dead, status() reported a STARTING daemon as crashed and a RUNNING one
    as stopped, on every poll, for every daemon.

    OpenProcess + GetExitCodeProcess is the supported probe. Merely being
    able to open a handle is not enough (a process that has exited stays
    openable while anything holds a handle to it), hence the exit-code
    check.
    """
    try:
        k32 = _win_kernel32()
        import ctypes
        from ctypes import wintypes
        handle = k32.OpenProcess(_WIN_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # Access denied means the process exists but isn't ours to
            # inspect; anything else (typically ERROR_INVALID_PARAMETER)
            # means there is no such pid.
            return _win_last_error() == _WIN_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.pointer(code)):
                return True    # opened it, couldn't read it: don't declare it dead
            return code.value == _WIN_STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — a probe that can't run must not crash status()
        # Same stance as tasks._pid_alive: a pid we can't check is assumed
        # alive, so a probe failure can't make a live daemon look dead.
        return True


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _on_windows():
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Still alive for our purposes.
        return True
    except OSError:
        return False
    return True


def _read_status(daemon_id):
    return atomic_io.read_json(status_path(daemon_id), default={}, expect=dict)


def _write_status(daemon_id, **fields):
    current = _read_status(daemon_id)
    current.update(fields)
    current["updated"] = time.time()
    atomic_io.write_json(status_path(daemon_id), current)
    return current


def _legacy_pid(daemon_id):
    path = _LEGACY_PID_FILES.get(daemon_id)
    if not path:
        return None
    try:
        pid = int(path.read_text(encoding=ENCODING).strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def status(daemon_id):
    """Live state for one daemon, reconciled against the actual OS.

    A status file alone is never trusted: a hard kill (the web Stop button
    is taskkill /T /F on Windows) never gets to write "stopped", so a
    stale "running" is the normal case rather than the exception. The pid
    is checked every time and the file is corrected when it disagrees —
    the same lease-style reasoning tasks.py uses.
    """
    did = normalize_id(daemon_id)
    entry = _load_registry().get(did) or {}
    state = _read_status(did)
    pid = state.get("child_pid")
    supervisor = state.get("supervisor_pid")

    running = pid_alive(pid)
    legacy = None
    if not running:
        legacy = _legacy_pid(did)
        running = legacy is not None

    reported = state.get("status") or STATUS_STOPPED
    if running:
        reported = STATUS_RUNNING
    elif reported == STATUS_STARTING:
        # H.1.1: start() writes STATUS_STARTING and returns immediately —
        # it does not wait for the supervisor to actually spawn the child
        # and write child_pid (see start()'s own docstring: "does not wait
        # for the child to be healthy"). Every single daemon start therefore
        # has a real gap where child_pid is still unset, pid_alive()
        # correctly says not-running, and status() used to read that as an
        # immediate crash — a false positive on 100% of starts, not just a
        # flaky one, since a web UI polling right after clicking Start is
        # exactly the case that hits this window every time.
        #
        # A supervisor that has already exited (crashed before ever
        # spawning its child, e.g. a broken interpreter path) is still
        # caught immediately via its own pid, no grace period needed for
        # that case. Only a supervisor that's alive but hasn't reported in
        # yet gets the benefit of the doubt, and only for a bounded window.
        supervisor_alive = pid_alive(supervisor)
        stuck = (time.time() - (state.get("updated") or 0)) > STARTING_GRACE_SECONDS
        if not supervisor_alive or stuck:
            reported = STATUS_STOPPED if state.get("stop_requested") else STATUS_CRASHED
            _write_status(did, status=reported, child_pid=None)
    elif reported == STATUS_RUNNING:
        # Claimed to be up and isn't. Distinguish a clean stop (we asked)
        # from a crash (we didn't), because that is the single most useful
        # thing to know when a gateway is mysteriously offline.
        #
        # H.1.2 (owner-reported, 2026-09-27): "we didn't ask for a stop" is
        # not, by itself, evidence of a crash. run_supervisor() always
        # writes the real final status itself — STOPPED or CRASHED, with a
        # reason — before it ever exits normally (see its own closing
        # _write_status call at the bottom of this module); that path never
        # reaches this auto-detection branch at all. The only way status()
        # finds RUNNING-but-gone with the *supervisor itself* also gone,
        # and no final status ever written, is something external to the
        # daemon: a host reboot, the supervisor being force-killed, or a
        # jarvis upgrade replacing the running process. Reporting every
        # daemon that was ever running as "crashed" the next time the panel
        # is opened after one of those is a false alarm on every row, not a
        # real crash signal — so with the supervisor also gone and no
        # stop_requested on file, this is now reported as stopped instead.
        # A supervisor that IS still alive with its child gone keeps the
        # original, deliberately immediate crashed reading unchanged (see
        # H.1.1's test_running_to_gone_is_still_an_immediate_crash_no_
        # regression) — that combination means the supervisor is actively
        # mid-run and something really did go wrong under it.
        if state.get("stop_requested"):
            reported = STATUS_STOPPED
        elif pid_alive(supervisor):
            reported = STATUS_CRASHED
        else:
            reported = STATUS_STOPPED
        _write_status(did, status=reported, child_pid=None)
    elif entry.get("next_start"):
        reported = STATUS_SCHEDULED

    return {
        "id": did,
        "status": reported,
        "running": running,
        "pid": legacy or (pid if running else None),
        "supervisor_pid": supervisor if pid_alive(supervisor) else None,
        "adopted": legacy is not None,
        "started_at": state.get("started_at"),
        "exit_code": state.get("exit_code"),
        "last_error": state.get("last_error") or "",
        "restarts": state.get("restarts") or 0,
        "next_start": entry.get("next_start"),
    }


def describe(entry):
    """Registry entry + live status, which is what every caller wants."""
    did = entry.get("id")
    out = dict(entry)
    out.update(status(did))
    out["console"] = str(console_path(did))
    out["has_console"] = console_path(did).exists()
    # Re-assert the registry's own fields: status() returns an "id" and a
    # "next_start" too, and the registry is authoritative for both.
    out["id"] = did
    out["builtin"] = bool(entry.get("builtin"))
    out["command"] = " ".join(_display_argv(entry.get("argv") or []))
    return out


def _display_argv(argv):
    return ["jarvis" if a == JARVIS_TOKEN else str(a) for a in argv]


# ---------------------------------------------------------------------------
# starting and stopping
# ---------------------------------------------------------------------------

def _jarvis_argv():
    """How to invoke this same jarvis. Mirrors scheduler/task_runner."""
    exe = os.environ.get("JARVIS_EXE")
    if exe:
        return [exe]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "jarvis"]


def resolve_argv(entry):
    argv = list(entry.get("argv") or [])
    if argv and argv[0] == JARVIS_TOKEN:
        return _jarvis_argv() + argv[1:]
    return [str(a) for a in argv]


# H.1.1: CREATE_NO_WINDOW alone is sufficient for a fully detached, invisible
# process — it creates the child with no console at all, which already
# achieves what DETACHED_PROCESS is for. Combining the two is a well-known
# Windows gotcha: they express contradictory intent (DETACHED_PROCESS says
# "no console, inherit nothing"; CREATE_NO_WINDOW says "create one, just
# keep it hidden"), and the observed symptom matched exactly what that
# contradiction predicts — a visible, empty console window on every daemon
# start. Factored out so it's testable without actually being on Windows.
def _detached_creationflags(is_windows=None):
    is_windows = (os.name == "nt") if is_windows is None else is_windows
    return CREATE_NO_WINDOW if is_windows else 0


def start(daemon_id):
    """Ask for a daemon to come up. Returns (ok, message).

    Returns as soon as the supervisor is spawned — it does not wait for the
    child to be healthy, because "healthy" for a gateway means a completed
    WebSocket handshake and nobody should block a CLI call on that. Check
    status() a moment later, or read the console.
    """
    did = normalize_id(daemon_id)
    entry = _load_registry().get(did)
    if not entry:
        return False, f"no daemon '{did}'"
    if not entry.get("enabled", True):
        return False, f"'{did}' is disabled — enable it first"

    current = status(did)
    if current["running"]:
        return False, f"'{did}' is already running (pid {current['pid']})"
    # H.1.3 (owner-reported, 2026-09-27): a daemon in STARTING or RESTARTING
    # (mid crash-loop backoff) is not `running` yet by the check above, but
    # its supervisor is already alive and already owns this daemon's child
    # slot — spawning a second supervisor here would race the first one for
    # the same console.log/status.json and stdin.queue. status()'s own
    # `supervisor_pid` field is already exactly "the recorded supervisor
    # pid, or None if it's not actually alive", so checking it catches this
    # without needing to special-case each intermediate status by name.
    if current["supervisor_pid"]:
        return False, (f"'{did}' already has a supervisor running "
                        f"(pid {current['supervisor_pid']}) — stop it first")

    argv = resolve_argv(entry)
    if not argv:
        return False, f"'{did}' has no command configured"

    # Clear a stale stop request, or the supervisor shuts down immediately.
    try:
        stop_flag_path(did).unlink()
    except OSError:
        pass

    supervisor_argv = _jarvis_argv() + ["daemon-run", did]
    env = dict(os.environ)
    env["JARVIS_DAEMON_ID"] = did

    creationflags = 0
    kwargs = {}
    if os.name == "nt":
        creationflags = _detached_creationflags(True)
    else:
        # New session, so the supervisor survives the terminal that started
        # it — the unix equivalent of DETACHED_PROCESS.
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            supervisor_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
            **kwargs,
        )
    except OSError as exc:
        _write_status(did, status=STATUS_STOPPED, last_error=str(exc))
        return False, f"could not start supervisor: {exc}"

    _write_status(did, status=STATUS_STARTING, supervisor_pid=proc.pid,
                  stop_requested=False, last_error="")
    # A one-shot scheduled start has fired; clear it so it doesn't re-fire
    # every tick from now until the end of time.
    if entry.get("next_start"):
        edit(did, next_start="")
    return True, f"starting '{did}' (supervisor pid {proc.pid})"


def _reap(pid):
    """Collect `pid` if it is OUR already-exited child (POSIX only).

    A supervisor that start() spawned from this same long-lived process (the
    Discord gateway, the web server's tool runner) stays a zombie until its
    parent waits on it, and pid_alive() counts a zombie as alive — so stop()
    would wait out its whole timeout for a supervisor that is really gone.
    A non-child (the normal case: each daemon op is its own process) raises
    ChildProcessError, which is ignored.
    """
    if os.name == "nt" or not pid:
        return
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError, ValueError):
        pass


def _is_other_process(pid):
    """True for a recorded supervisor that is a genuinely separate process.

    A supervisor run as a thread of THIS process (tests; run_supervisor()
    called directly) records our own pid. It can never "exit" while we wait
    for it, and the force-kill fallback below must never signal ourselves.
    """
    return bool(pid) and int(pid) != os.getpid()


def stop(daemon_id, timeout=None):
    """Ask a daemon to shut down, then insist. Returns (ok, message).

    `timeout` defaults to the daemon's own stop_timeout, so a service that
    is known to take 30s to flush doesn't get killed at 10 just because the
    caller didn't say otherwise.
    """
    did = normalize_id(daemon_id)
    entry = _load_registry().get(did) or {}
    if timeout is None:
        timeout = int(entry.get("stop_timeout") or DEFAULT_STOP_TIMEOUT)
    current = status(did)
    if not current["running"] and not current["supervisor_pid"]:
        return False, f"'{did}' is not running"

    # The flag is the polite request: the supervisor sees it, closes the
    # child's stdin, signals it, and records a clean exit. Everything below
    # is the fallback for a supervisor that is itself gone.
    try:
        stop_flag_path(did).write_text(str(time.time()), encoding=ENCODING)
    except OSError:
        pass
    _write_status(did, stop_requested=True)

    # L.26: "stopped" means the child AND its supervisor are gone. The
    # supervisor outlives its child (it still joins the pump threads, writes
    # the exit line and the final status), so declaring success on the child
    # alone let restart() call start() while the old supervisor was alive and
    # lose to the H.1.3 guard every time.
    deadline = time.time() + timeout
    while time.time() < deadline:
        _reap(_read_status(did).get("supervisor_pid"))
        now = status(did)
        if not now["running"] and not (
                now["supervisor_pid"] and _is_other_process(now["supervisor_pid"])):
            return True, f"'{did}' stopped"
        time.sleep(0.25)

    # Still up. Kill the child directly — this covers an adopted daemon
    # (started the old way, no supervisor watching a flag) as well as a
    # supervisor that has wedged.
    sig = entry.get("stop_signal") or "TERM"
    killed = []
    for pid in (status(did).get("pid"), _read_status(did).get("supervisor_pid")):
        if pid and _is_other_process(pid) and pid_alive(pid):
            if _terminate(pid, sig):
                killed.append(pid)
    _write_status(did, status=STATUS_STOPPED, child_pid=None)
    if killed:
        return True, f"'{did}' force-stopped (pid {', '.join(str(k) for k in killed)})"
    return False, f"'{did}' did not stop within {timeout}s"


def _terminate(pid, sig="TERM"):
    """Best-effort stop. Windows has no signals, so taskkill /T /F is the
    only reliable option there and `sig` is ignored — documented rather than
    silently different, because "stop_signal: INT" appearing to work on
    Windows and not actually being sent is worse than it plainly not
    applying."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        else:
            os.kill(int(pid), STOP_SIGNALS.get(sig, STOP_SIGNALS["TERM"]))
            for _ in range(20):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
            # SIGKILL is the backstop whatever the configured signal was:
            # a daemon that ignores its polite signal must still be
            # stoppable, or "stop" becomes advisory.
            if pid_alive(pid):
                os.kill(int(pid), STOP_SIGNALS["KILL"])
        return True
    except Exception:  # noqa: BLE001 — a failed kill is reported, never raised
        return False


# How long restart() waits for a stopping supervisor to finish exiting after
# stop() reported success. stop() already waits for it, so this is only a
# belt-and-braces bound for the adopted/force-killed paths.
RESTART_SUPERVISOR_WAIT = 5.0


def restart(daemon_id, timeout=None):
    """Stop (if anything is up) and start again. Returns (ok, message).

    L.26: stop whenever the child is running OR a supervisor is alive — a
    `starting` / `restarting` (crash-loop backoff) daemon has no running
    child but its supervisor still owns the slot, and start() refuses while
    one is alive. Then wait for that supervisor to actually be gone before
    starting, so the H.1.3 guard (which stays) only ever refuses a genuine
    second supervisor. A failure after the stop says what state the daemon
    was left in rather than leaving it silently stopped.
    """
    did = normalize_id(daemon_id)
    before = status(did)
    stopped_first = False
    if before["running"] or before["supervisor_pid"]:
        ok, msg = stop(did, timeout=timeout)
        if not ok:
            now = status(did)
            state = "still running" if now["running"] else now["status"]
            return False, (f"could not stop before restart: {msg} "
                           f"('{did}' is {state})")
        stopped_first = True
        deadline = time.time() + RESTART_SUPERVISOR_WAIT
        while time.time() < deadline:
            _reap(_read_status(did).get("supervisor_pid"))
            sup = status(did)["supervisor_pid"]
            if not (sup and _is_other_process(sup)):
                break
            time.sleep(0.1)
    ok, msg = start(did)
    if not ok and stopped_first:
        return False, f"stopped '{did}' but could not start it again: {msg}"
    return ok, msg


# ---------------------------------------------------------------------------
# console + stdin
# ---------------------------------------------------------------------------

def read_console(daemon_id, lines=200, path=None):
    """Tail the console. `path` reads one of the rotated backups instead.

    Reads the tail without loading the whole file: a gateway's console can
    be megabytes and the caller almost always wants the last screenful.
    """
    target = Path(path) if path else console_path(daemon_id)
    if not target.exists():
        return []
    try:
        size = target.stat().st_size
        # ~200 bytes/line is a generous estimate; re-read from the start if
        # the guess was too small rather than looping.
        window = min(size, max(8192, lines * 200))
        with target.open("rb") as fh:
            if size > window:
                fh.seek(size - window)
                fh.readline()  # drop the partial first line
            data = fh.read()
    except OSError as exc:
        return [f"(could not read console: {exc})"]
    text = data.decode(ENCODING, errors="replace")
    return text.splitlines()[-lines:]


def send_input(daemon_id, text):
    """Queue one line for the daemon's stdin. Returns (ok, message)."""
    did = normalize_id(daemon_id)
    entry = _load_registry().get(did)
    if not entry:
        return False, f"no daemon '{did}'"
    if not entry.get("supports_stdin"):
        return False, (f"'{did}' doesn't accept console input. Set "
                       f"supports_stdin on it if its process reads stdin.")
    if not status(did)["running"]:
        return False, f"'{did}' is not running"
    line = str(text or "").rstrip("\n")
    try:
        with stdin_queue_path(did).open("a", encoding=ENCODING) as fh:
            fh.write(line + "\n")
    except OSError as exc:
        return False, f"could not queue input: {exc}"
    return True, "queued"


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------

def schedule(daemon_id, when_text):
    """Set (or clear, with an empty string) a one-shot next start time.

    Parsing is delegated to timespec.py so "in 20 minutes", "tomorrow 8am"
    and an ISO timestamp all work the same way they do for reminders —
    there is no reason for a second time grammar in the same product.
    """
    did = normalize_id(daemon_id)
    if not _load_registry().get(did):
        return False, f"no daemon '{did}'"
    if not str(when_text or "").strip():
        edit(did, next_start="")
        return True, "cleared"
    from . import timespec
    try:
        when = timespec.parse_when(when_text)
    except timespec.TimeSpecError as exc:
        return False, f"could not understand '{when_text}': {exc}"
    iso = timespec.to_iso(when)
    ok, err = edit(did, next_start=iso)
    return (True, iso) if ok else (False, err)


def due_now(now=None):
    """Registered daemons whose next_start has arrived and which are down.

    Called from the scheduler's tick, so scheduled starts work with exactly
    the machinery that already exists rather than a second timer loop.
    """
    now = time.time() if now is None else now
    out = []
    for entry in _load_registry().values():
        when = entry.get("next_start")
        if not when or not entry.get("enabled", True):
            continue
        try:
            from . import timespec
            ts = timespec.from_iso(when).timestamp()
        except Exception:  # noqa: BLE001 — an unparseable stored time must
            # not wedge the tick for every other daemon; skip just this one.
            continue
        if ts > now:
            continue
        if status(entry["id"])["running"]:
            continue
        out.append(entry["id"])
    return out


def tick(now=None):
    """Start everything that is due. Returns a list of (id, ok, message)."""
    results = []
    for did in due_now(now=now):
        ok, msg = start(did)
        results.append({"id": did, "ok": ok, "message": msg})
    return results


def autostart_all():
    """Bring up every daemon flagged autostart that isn't already up.

    L.27: this is called from the scheduler's STARTUP tick (see
    scheduler.tick), which is the one place every long-lived driver — the
    web server, `jarvis sched-daemon`, `jarvis sched-tick --startup` from
    Task Scheduler — already agrees means "Jarvis just started".

    Safe to call twice. A daemon is left alone if its child is running OR if
    its supervisor is alive (STARTING, or RESTARTING in crash-loop backoff):
    start() would refuse the latter with the H.1.3 guard, and "already being
    brought up" is not a failure worth reporting. A real failure is recorded
    on the daemon's own status (last_error) so the Daemons panel shows it
    instead of the daemon silently staying down.
    """
    results = []
    for entry in _load_registry().values():
        if not entry.get("autostart") or not entry.get("enabled", True):
            continue
        current = status(entry["id"])
        if current["running"] or current["supervisor_pid"]:
            continue
        ok, msg = start(entry["id"])
        if not ok:
            _write_status(entry["id"], status=STATUS_STOPPED,
                          last_error=f"autostart failed: {msg}")
        results.append({"id": entry["id"], "ok": ok, "message": msg})
    return results


# ---------------------------------------------------------------------------
# the supervisor  (`jarvis daemon-run <id>`)
# ---------------------------------------------------------------------------

def _rotate_console(path):
    try:
        if not path.exists() or path.stat().st_size < MAX_CONSOLE_BYTES:
            return
    except OSError:
        return
    try:
        for i in range(MAX_CONSOLE_BACKUPS - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            dst = path.with_suffix(path.suffix + f".{i + 1}")
            if src.exists():
                src.replace(dst)
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def _console_write(handle, text):
    try:
        handle.write(text)
        handle.flush()
    except (OSError, ValueError):
        pass


def _stamp(line):
    return f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}"


def run_supervisor(daemon_id):
    """Foreground supervisor for one daemon. Returns a process exit code.

    This IS the long-lived process; `daemon-start` spawns it detached.
    Everything it does is observable from disk, so a second `jarvis`
    invocation can report on it, feed it, and stop it.
    """
    did = normalize_id(daemon_id)
    entry = _load_registry().get(did)
    if not entry:
        print(f"no daemon '{did}'", file=sys.stderr)
        return 1

    argv = resolve_argv(entry)
    if not argv:
        print(f"'{did}' has no command configured", file=sys.stderr)
        return 1

    # Refuse to double-run. Two supervisors on one daemon means the child
    # is started twice — for a Discord gateway that is every message
    # answered twice, the exact failure discord_gateway's own PID file
    # exists to prevent.
    existing = status(did)
    if existing["running"]:
        print(f"'{did}' is already running (pid {existing['pid']})", file=sys.stderr)
        return 1

    console = console_path(did)
    _rotate_console(console)
    queue = stdin_queue_path(did)
    flag = stop_flag_path(did)
    try:
        queue.unlink()
    except OSError:
        pass

    cwd = entry.get("cwd") or None
    if cwd and not Path(cwd).is_dir():
        _write_status(did, status=STATUS_CRASHED,
                      last_error=f"working directory does not exist: {cwd}")
        print(f"working directory does not exist: {cwd}", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (entry.get("env") or {}).items()})
    env["JARVIS_DAEMON_ID"] = did

    try:
        handle = console.open("a", encoding=ENCODING, errors="replace")
    except OSError as exc:
        print(f"could not open console log: {exc}", file=sys.stderr)
        return 1

    wants_stdin = bool(entry.get("supports_stdin"))
    use_shell = bool(entry.get("shell"))
    stop_signal = entry.get("stop_signal") or "TERM"
    policy = entry.get("restart") or RESTART_NEVER
    restart_delay = max(1, int(entry.get("restart_delay") or DEFAULT_RESTART_DELAY))
    max_restarts = max(0, int(entry.get("max_restarts")
                              if entry.get("max_restarts") is not None
                              else DEFAULT_MAX_RESTARTS))

    import threading

    # The restart loop. One iteration is one child process; the loop only
    # goes round again when the policy says so, the stop flag is absent, and
    # the windowed retry budget still has room. Everything about the child
    # is recreated each pass because a respawn has to be a genuinely fresh
    # process, not a reused handle.
    restarts = []          # epoch times, trimmed to RESTART_WINDOW_SECONDS
    stopping = False
    code = 0
    final = STATUS_STOPPED
    error = ""

    while True:
        shown = " ".join(argv) if not use_shell else argv
        _console_write(handle, _stamp(f"=== starting: {shown}") + "\n")
        try:
            proc = subprocess.Popen(
                # shell=True wants the command as ONE string, and add()
                # stored it that way unsplit (see the note there). A
                # multi-element argv on a shell daemon can only come from a
                # list passed directly, so joining is the right fallback.
                ((argv[0] if len(argv) == 1 else " ".join(argv))
                 if use_shell else argv),
                shell=use_shell,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if wants_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
                encoding=ENCODING,
                errors="replace",
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            _console_write(handle, _stamp(f"=== failed to start: {exc}") + "\n")
            handle.close()
            _write_status(did, status=STATUS_CRASHED, last_error=str(exc),
                          child_pid=None)
            return 1

        started = time.time()
        _write_status(did, status=STATUS_RUNNING, child_pid=proc.pid,
                      supervisor_pid=os.getpid(), started_at=started,
                      stop_requested=False, exit_code=None, last_error="",
                      restarts=len(restarts))

        # stdout/stderr are drained on their own threads, one each. Doing it
        # inline would mean the stdin queue is only checked between output
        # lines, so a silent daemon would never receive anything typed at it.
        #
        # H.1 (Daemons UI rework): stdout and stderr used to be merged at the
        # OS level (stderr=STDOUT) before either byte reached this process,
        # which threw the distinction away for good — no amount of frontend
        # cleverness can recover it after the fact. They are now two real
        # pipes and two pump threads; the "E: " prefix is the one place that
        # distinction survives into the flat console.log file, and it is
        # deliberately a plain, greppable text marker rather than a control
        # byte, so `tail`/`grep -v '^E: '` on the raw file still works. The
        # web console (daemons.js) strips it back off for display and uses
        # it, plus a traceback-shaped-line check, to colour stdout / stderr /
        # crash traceback separately.
        def _pump(stream, prefix=""):
            try:
                for line in stream:
                    text = line if line.endswith("\n") else line + "\n"
                    _console_write(handle, (prefix + text) if prefix else text)
            except (OSError, ValueError):
                pass

        pump_out = threading.Thread(target=_pump, args=(proc.stdout,), daemon=True)
        pump_err = threading.Thread(target=_pump, args=(proc.stderr, "E: "), daemon=True)
        pump_out.start()
        pump_err.start()

        def _drain_queue(child=proc):
            if not wants_stdin or not queue.exists():
                return
            try:
                text = queue.read_text(encoding=ENCODING)
                queue.unlink()
            except OSError:
                return
            for line in text.splitlines():
                try:
                    child.stdin.write(line + "\n")
                    child.stdin.flush()
                    _console_write(handle, _stamp(f"<<< {line}") + "\n")
                except (OSError, ValueError, AttributeError):
                    return

        asked_to_stop = False
        try:
            while True:
                if proc.poll() is not None:
                    break
                if flag.exists() and not asked_to_stop:
                    asked_to_stop = True
                    stopping = True
                    _console_write(handle, _stamp("=== stop requested") + "\n")
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                    except (OSError, ValueError):
                        pass
                    _terminate(proc.pid, stop_signal)
                _drain_queue()
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            stopping = True
            asked_to_stop = True
            _terminate(proc.pid, stop_signal)

        pump_out.join(timeout=2)
        pump_err.join(timeout=2)
        code = proc.returncode
        elapsed = time.time() - started
        _console_write(handle,
                       _stamp(f"=== exited with code {code} after {elapsed:.1f}s") + "\n")

        if stopping:
            final, error = STATUS_STOPPED, ""
            break
        if code == 0:
            final, error = STATUS_STOPPED, ""
        else:
            final = STATUS_CRASHED
            error = f"exited with code {code}"
            if elapsed < FAST_EXIT_SECONDS:
                # Almost always a config problem rather than a runtime one,
                # and saying so here saves reading the console to find out.
                tail = [ln for ln in read_console(did, lines=8) if ln.strip()]
                last = tail[-1] if tail else "(none)"
                if last.startswith("E: "):
                    last = last[3:]  # the "came from stderr" marker, not part of the message
                error += (f" after {elapsed:.1f}s — it failed to start rather "
                          f"than stopping. Last output: " + last)

        # --- should it come back? ------------------------------------------
        if policy == RESTART_NEVER:
            break
        if policy == RESTART_ON_FAILURE and code == 0:
            break

        now = time.time()
        restarts = [t for t in restarts if now - t < RESTART_WINDOW_SECONDS]
        if max_restarts and len(restarts) >= max_restarts:
            error = (f"restart limit reached ({max_restarts} in "
                     f"{RESTART_WINDOW_SECONDS // 60} minutes) — not restarting "
                     f"again. Last exit code {code}.")
            _console_write(handle, _stamp("=== " + error) + "\n")
            final = STATUS_CRASHED
            break
        restarts.append(now)

        _write_status(did, status=STATUS_RESTARTING, child_pid=None,
                      exit_code=code, last_error=error, restarts=len(restarts))
        _console_write(handle, _stamp(
            f"=== restarting in {restart_delay}s "
            f"(attempt {len(restarts)}/{max_restarts or '\u221e'})") + "\n")

        # The delay is slept in short slices so a stop request during the
        # backoff is honoured promptly instead of after the full wait.
        waited = 0.0
        while waited < restart_delay:
            if flag.exists():
                stopping = True
                break
            time.sleep(min(0.4, restart_delay - waited))
            waited += 0.4
        if stopping:
            final, error = STATUS_STOPPED, ""
            break
        _rotate_console(console)

    handle.close()

    try:
        flag.unlink()
    except OSError:
        pass
    _write_status(did, status=final, child_pid=None, exit_code=code,
                  last_error=error, stop_requested=False,
                  restarts=len(restarts))
    return 0 if final == STATUS_STOPPED else 1
