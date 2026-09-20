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

from . import atomic_io

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
        "supports_stdin": False,
        "enabled": True,
    },
    "discord": {
        "id": "discord",
        "name": "Discord gateway",
        "builtin": True,
        "argv": [JARVIS_TOKEN, "discord-daemon"],
        "description": "Holds the Discord WebSocket open and answers messages.",
        "supports_stdin": False,
        "enabled": True,
    },
    "instagram": {
        "id": "instagram",
        "name": "Instagram webhook",
        "builtin": True,
        "argv": [JARVIS_TOKEN, "instagram-serve"],
        "description": "Receives Instagram DMs over the Meta webhook.",
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
        merged[did] = entry
    for did, stored in entries.items():
        if did in BUILTINS or not isinstance(stored, dict):
            continue
        entry = dict(stored)
        entry["id"] = did
        entry["builtin"] = False
        merged[did] = entry
    return merged


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


def add(daemon_id, command, name="", cwd="", env=None, supports_stdin=False,
        description="", autostart=False, shell=False,
        restart=RESTART_NEVER, restart_delay=DEFAULT_RESTART_DELAY,
        max_restarts=DEFAULT_MAX_RESTARTS, stop_signal="TERM",
        stop_timeout=DEFAULT_STOP_TIMEOUT, notes=""):
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
                argv = shlex.split(command, posix=(os.name != "nt"))
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
             "stop_timeout")

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
                    value = shlex.split(value, posix=(os.name != "nt"))
                except ValueError as exc:
                    return False, f"could not parse command: {exc}"
        if key == "restart" and value not in RESTART_POLICIES:
            return False, ("restart must be one of: "
                           + ", ".join(RESTART_POLICIES))
        if key == "stop_signal" and value not in STOP_SIGNALS:
            return False, "stop_signal must be one of: " + ", ".join(STOP_SIGNALS)
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

def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
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
    elif reported in (STATUS_RUNNING, STATUS_STARTING):
        # Claimed to be up and isn't. Distinguish a clean stop (we asked)
        # from a crash (we didn't), because that is the single most useful
        # thing to know when a gateway is mysteriously offline.
        reported = STATUS_STOPPED if state.get("stop_requested") else STATUS_CRASHED
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
        creationflags = CREATE_NO_WINDOW | DETACHED_PROCESS
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

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not status(did)["running"]:
            return True, f"'{did}' stopped"
        time.sleep(0.25)

    # Still up. Kill the child directly — this covers an adopted daemon
    # (started the old way, no supervisor watching a flag) as well as a
    # supervisor that has wedged.
    sig = entry.get("stop_signal") or "TERM"
    killed = []
    for pid in (status(did).get("pid"), _read_status(did).get("supervisor_pid")):
        if pid and pid_alive(pid):
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


def restart(daemon_id, timeout=None):
    did = normalize_id(daemon_id)
    if status(did)["running"]:
        ok, msg = stop(did, timeout=timeout)
        if not ok:
            return False, f"could not stop before restart: {msg}"
    return start(did)


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
    """Bring up every daemon flagged autostart that isn't already up."""
    results = []
    for entry in _load_registry().values():
        if not entry.get("autostart") or not entry.get("enabled", True):
            continue
        if status(entry["id"])["running"]:
            continue
        ok, msg = start(entry["id"])
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
                stderr=subprocess.STDOUT,
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

        # stdout is drained on its own thread. Doing it inline would mean the
        # stdin queue is only checked between output lines, so a silent
        # daemon would never receive anything typed at it.
        def _pump(stream=proc.stdout):
            try:
                for line in stream:
                    _console_write(handle, line if line.endswith("\n") else line + "\n")
            except (OSError, ValueError):
                pass

        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()

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

        pump.join(timeout=2)
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
                error += (f" after {elapsed:.1f}s — it failed to start rather "
                          f"than stopping. Last output: "
                          + (tail[-1] if tail else "(none)"))

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
