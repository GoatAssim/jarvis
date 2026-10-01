"""Auto-discovered tool: shut down, restart, sleep or lock this PC (L.16, Q-L16b).

Why this exists: a scheduled job saying "...then shut down the PC" was
impossible, because the repo had no power tool at all. The router matched no
group for "shutdown" and the model improvised with clicks instead.

HOW CONFIRMATION WORKS — read before changing anything here
-----------------------------------------------------------
This tool does NOT ask the user anything itself. The gate is out of band, the
same as every other mutating tool:

  * Interactive ask: TOOL_CONFIRM_REQUIRED below puts `power_action` in
    tool_safety.DEFAULT_CONFIRM_REQUIRED, so ai_client's tool executor shows
    the confirmation prompt before this handler ever runs (owner decision
    Q-L16b: "confirm when the owner asks").
  * Scheduled job: scheduler._needs_approval() sees the same confirm flag and
    parks the job at status=needs_approval at CREATION time. A person approves
    it once; at fire time scheduler._do_tool() calls execute_tool directly and
    there is deliberately no second prompt (owner decision Q-L16b: "a
    scheduled task does not need confirmation").

A model-fillable `confirm: true` argument would be a fake gate, so there is
none. See actions/_template.py section 4.

WHY SHUTDOWN / RESTART HAVE A GRACE PERIOD
------------------------------------------
shutdown and restart default to a 15 second delay (Windows `shutdown /t`).
Two reasons: the scheduler and the notifier get time to log the run and send
"shutting down" to the owner's phone before the machine goes away, and the
owner has a window to say `cancel` if a prompt-injected or mistaken request
slipped through. Pass delay_seconds=0 to go immediately.

`force` is off by default. Without it Windows may sit at "closing apps" if a
program has unsaved work; with it, unsaved work is lost. An unattended job
that must reliably power off can ask for force=true — that is a decision for
whoever approved the job, which is why it is a visible argument and not a
silent default.

PLATFORMS
---------
Windows is the supported target (every other tool in this repo is Windows
first). Linux and macOS command lines are built too, but have only been
checked as strings, never run — see tests/test_power_tools.py.

JARVIS_POWER_DRY_RUN=1 makes the handler return the exact command it WOULD
have run and execute nothing. The checklist's "run" steps use it so a tester
can verify the tool without losing their session.
"""

import math
import os
import platform
import subprocess

ACTIONS = ("shutdown", "restart", "sleep", "lock", "cancel")
# Only these two actions are delayed (and therefore cancellable).
_DELAYABLE = ("shutdown", "restart")
DEFAULT_DELAY_SECONDS = 15
MAX_DELAY_SECONDS = 3600
_RUN_TIMEOUT = 20

_SLEEP_PS = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.Application]::SetSuspendState("
    "[System.Windows.Forms.PowerState]::Suspend, $false, $false)"
)


def _system():
    return platform.system()


def _clamp_delay(value, action):
    """Seconds as a plain int within bounds, or the default for this action.

    Non-delayable actions always return 0 — a delay on `sleep` or `lock` would
    be silently ignored by the OS command, so reporting one would be a lie.
    """
    if action not in _DELAYABLE:
        return 0
    if value is None or value == "":
        return DEFAULT_DELAY_SECONDS
    if isinstance(value, bool):
        raise ValueError("delay_seconds must be a number of seconds")
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        raise ValueError("delay_seconds must be a number of seconds") from None
    if seconds < 0:
        raise ValueError("delay_seconds can't be negative")
    return min(seconds, MAX_DELAY_SECONDS)


def build_command(action, delay_seconds=0, force=False, system=None):
    """argv list for one power action on one OS. Pure — runs nothing.

    Returns None when the action has no equivalent on that OS.
    """
    system = system or _system()
    delay = int(delay_seconds or 0)

    if system == "Windows":
        if action in _DELAYABLE:
            flag = "/s" if action == "shutdown" else "/r"
            argv = ["shutdown", flag, "/t", str(delay)]
            if force:
                argv.append("/f")
            return argv
        if action == "cancel":
            return ["shutdown", "/a"]
        if action == "lock":
            return ["rundll32.exe", "user32.dll,LockWorkStation"]
        if action == "sleep":
            # Not rundll32 powrprof.dll,SetSuspendState: that call hibernates
            # instead of sleeping whenever hibernation is enabled, which is
            # the default on most laptops. This is the real "sleep".
            return ["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", _SLEEP_PS]
        return None

    if system == "Darwin":
        if action == "shutdown":
            return (["osascript", "-e", 'tell app "System Events" to shut down']
                    if delay == 0 else
                    ["shutdown", "-h", "+%d" % max(1, math.ceil(delay / 60))])
        if action == "restart":
            return (["osascript", "-e", 'tell app "System Events" to restart']
                    if delay == 0 else
                    ["shutdown", "-r", "+%d" % max(1, math.ceil(delay / 60))])
        if action == "sleep":
            return ["pmset", "sleepnow"]
        if action == "lock":
            return ["pmset", "displaysleepnow"]
        if action == "cancel":
            return ["killall", "shutdown"]
        return None

    # Linux and anything else systemd-shaped.
    if action in _DELAYABLE:
        if delay == 0:
            return ["systemctl", "poweroff" if action == "shutdown" else "reboot"]
        # shutdown(8) counts in whole minutes, so round UP: never earlier
        # than asked.
        minutes = max(1, math.ceil(delay / 60))
        return ["shutdown", "-P" if action == "shutdown" else "-r",
                "+%d" % minutes]
    if action == "cancel":
        return ["shutdown", "-c"]
    if action == "sleep":
        return ["systemctl", "suspend"]
    if action == "lock":
        return ["loginctl", "lock-session"]
    return None


def _run(argv):
    """The one place a process is started. Tests replace this."""
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=_RUN_TIMEOUT, shell=False)


def tool_power_action(args):
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    # Be forgiving about the model's phrasing; the schema enum is the contract
    # but "power off" / "reboot" are what people actually say.
    action = {"poweroff": "shutdown", "power off": "shutdown",
              "power_off": "shutdown", "off": "shutdown",
              "reboot": "restart", "suspend": "sleep",
              "abort": "cancel"}.get(action, action)
    if action not in ACTIONS:
        return {"ok": False,
                "error": "action must be one of: %s" % ", ".join(ACTIONS)}

    try:
        delay = _clamp_delay(args.get("delay_seconds"), action)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    force = bool(args.get("force")) and action in _DELAYABLE

    system = _system()
    argv = build_command(action, delay, force, system)
    if argv is None:
        return {"ok": False,
                "error": "'%s' isn't supported on %s" % (action, system)}

    if os.environ.get("JARVIS_POWER_DRY_RUN"):
        return {"ok": True, "dry_run": True, "action": action,
                "delay_seconds": delay, "force": force,
                "command": argv,
                "note": "JARVIS_POWER_DRY_RUN is set — nothing was run."}

    try:
        proc = _run(argv)
    except FileNotFoundError:
        return {"ok": False, "action": action,
                "error": "couldn't start %s — not found on this PC" % argv[0]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "action": action,
                "error": "the %s command didn't return within %ds"
                         % (action, _RUN_TIMEOUT)}
    except OSError as exc:
        return {"ok": False, "action": action, "error": str(exc)}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        # `shutdown /a` exits 1116 when nothing was pending. That is not a
        # failure worth alarming anyone about.
        if action == "cancel" and proc.returncode == 1116:
            return {"ok": True, "action": "cancel", "cancelled": False,
                    "note": "No shutdown or restart was pending."}
        return {"ok": False, "action": action,
                "error": "%s failed (exit %s)%s"
                         % (action, proc.returncode,
                            ": " + detail if detail else "")}

    result = {"ok": True, "action": action, "delay_seconds": delay}
    if action in _DELAYABLE:
        result["force"] = force
        result["note"] = (
            "PC will %s in %d second(s). Call power_action with action='cancel' "
            "to stop it." % ("shut down" if action == "shutdown" else "restart",
                              delay) if delay else
            "PC is %s now." % ("shutting down" if action == "shutdown"
                               else "restarting"))
    elif action == "cancel":
        result["cancelled"] = True
    return result


TOOL_SCHEMAS = [
    {
        "name": "power_action",
        "description": (
            "Shut down, restart, sleep or lock this PC, or cancel a pending "
            "shutdown/restart. Use for 'shut down the PC', 'turn off the "
            "computer', 'restart', 'put it to sleep', 'lock the screen'. "
            "shutdown and restart wait delay_seconds (default 15) so there is "
            "time to cancel; sleep and lock act immediately. This does NOT "
            "close or save anything for the user — do not use it as a way to "
            "close one program."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": "What to do to the PC.",
                },
                "delay_seconds": {
                    "type": "integer",
                    "description": (
                        "shutdown/restart only: seconds to wait first "
                        "(default 15, 0 = immediately, max 3600)."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "shutdown/restart only: close running programs even "
                        "if they have unsaved work. Default false."
                    ),
                },
            },
            "required": ["action"],
        },
    },
]

TOOLS = {"power_action": tool_power_action}

# Joins the existing group (radio, packages, git). Its siblings are offered
# whenever this group is routed, and the keywords below make "shutdown" route
# here at all — before this, no group matched it.
TOOL_GROUP = "system_control"

TOOL_KEYWORDS = {
    "power_action": {
        "shutdown": 10, "shut down": 10, "shut it down": 10,
        "turn off the pc": 10, "turn off the computer": 10,
        "turn off my pc": 10, "turn off my computer": 10,
        "power off": 10, "power down": 10,
        "restart the pc": 10, "restart the computer": 10,
        "restart my pc": 10, "reboot": 9,
        "put the pc to sleep": 10, "put it to sleep": 9,
        "go to sleep": 7, "sleep mode": 8,
        "lock the pc": 10, "lock the computer": 10, "lock the screen": 10,
        "lock my pc": 10, "cancel the shutdown": 10,
    },
}

# The real gate — see the module docstring. Merged into
# tool_safety.DEFAULT_CONFIRM_REQUIRED by tools.py at import time, so
# tool_safety.py itself is untouched.
TOOL_CONFIRM_REQUIRED = {"power_action"}
TOOL_AI_REVIEW = set()

# No inline TEST_CHECKLIST: this ships with jarvis, so its entry lives in
# web/public/test-checklist-data.js (group "system_control", next to wifi_set),
# the source of truth for shipped tools. See recent_dms.py for the same note.
