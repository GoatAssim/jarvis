"""Notification delivery — the "how it reaches you" half of the scheduler.

scheduler.py decides WHEN something happens. This module decides how the
user actually finds out, across however many surfaces are in play (a
browser tab, a terminal, the OS notification centre, Playnite, a speaker).

THE DURABLE INBOX IS THE POINT
------------------------------
Every other channel here is fire-and-forget: a toast pops whether or not
anyone is at the machine, and a WebSocket broadcast reaches only the tabs
open *right now*. For a reminder, that's not good enough — "remind me at 9"
firing into a closed browser is indistinguishable from never having fired.

So delivery is split in two:

  * The INBOX (~/.jarvis/notifications.json) is durable and always written.
    It's a queue, not a log: the web console drains it on connect and on an
    interval, `jarvis ask` drains it at the top of a reply, and each
    consumer acknowledges what it consumed. A notification raised while
    nothing was running is still waiting the next time anything is.

  * LIVE channels (toast/stream/voice/playnite) are best-effort extras on
    top. Each is wrapped so an unavailable one (no Playnite running, no TTS
    installed, no notify-send on this distro) degrades to "not delivered
    that way" and never takes down the tick that raised it.

CHANNEL SELECTION
-----------------
Per-notification `channels` wins; otherwise the defaults in
~/.jarvis/notify_config.json apply, which are per-KIND — a reminder
probably wants a toast, a routine background task probably doesn't. Same
plain-JSON, created-on-first-use, re-read-every-call approach as
tool_safety.py and ai_config.py, so there's no reload step and the file is
hand-editable.
"""

import json
import os
import shutil
import subprocess
import sys
import secrets
from datetime import datetime
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
INBOX_FILE = JARVIS_DIR / "notifications.json"
CONFIG_FILE = JARVIS_DIR / "notify_config.json"
ENCODING = "utf-8"

CHANNELS = ("inbox", "stream", "toast", "voice", "playnite",
            "discord", "instagram")

# Cap on retained notifications. The inbox is a queue — unacknowledged items
# are kept, but a consumer that never acknowledges (a browser nobody opens
# again) shouldn't grow the file without bound.
MAX_INBOX = 200
MAX_MESSAGE_CHARS = 2000
TOAST_TIMEOUT = 15

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_CONFIG = {
    "enabled": True,
    # Per-kind default channels. "inbox" is in every one of these on
    # purpose: it's the only channel that survives nothing being open.
    "channels": {
        "reminder": ["inbox", "stream", "toast"],
        "notify": ["inbox", "stream", "toast"],
        "task": ["inbox", "stream"],
    },
    # Windows toasts go through PowerShell's BurntToast module when it's
    # installed (much nicer looking), falling back to a plain balloon via
    # .NET's NotifyIcon, which needs nothing installed at all.
    "prefer_burnt_toast": True,
    "voice_enabled": False,
}


def _load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        data = {}
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "channels" and isinstance(value, dict):
                config["channels"].update(value)
            else:
                config[key] = value
    return config


def ensure_config():
    """Write the default config out if it doesn't exist yet, and return its
    path — mirrors ai_config.ensure_ai_config() so `jarvis notify-config`
    can print a path the user can immediately open and edit."""
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            CONFIG_FILE.write_text(
                json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding=ENCODING)
    except OSError:
        pass
    return CONFIG_FILE


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def _load_inbox():
    try:
        data = json.loads(INBOX_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_inbox(items):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INBOX_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items[-MAX_INBOX:], indent=2, default=str) + "\n",
                       encoding=ENCODING)
        os.replace(str(tmp), str(INBOX_FILE))
        return True
    except OSError:
        return False


def pending(consumer="web", limit=50):
    """Undelivered notifications for one consumer, oldest first.

    Acknowledgement is tracked PER CONSUMER (`seen_by`), not as a single
    delivered flag, because the web console and a terminal are genuinely
    different places a person might be looking — a reminder the browser
    already showed should still print in a terminal session that hasn't
    seen it, and vice versa.
    """
    consumer = (consumer or "web").strip() or "web"
    out = []
    for item in _load_inbox():
        if consumer in (item.get("seen_by") or []):
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def acknowledge(ids, consumer="web"):
    """Mark notifications as seen by one consumer. Fully-acknowledged old
    items are pruned here rather than on a timer — this is the only place
    that knows every consumer is done with them."""
    consumer = (consumer or "web").strip() or "web"
    wanted = set(ids or [])
    items = _load_inbox()
    touched = 0
    for item in items:
        if item.get("id") in wanted:
            seen = list(item.get("seen_by") or [])
            if consumer not in seen:
                seen.append(consumer)
                item["seen_by"] = seen
                touched += 1
    _save_inbox(items)
    return touched


def clear(consumer=None):
    """Drop the inbox. With a consumer, only marks everything seen by them."""
    if consumer:
        items = _load_inbox()
        return acknowledge([i.get("id") for i in items], consumer)
    count = len(_load_inbox())
    _save_inbox([])
    return count


def history(limit=50):
    return list(reversed(_load_inbox()))[:limit]


# ---------------------------------------------------------------------------
# The one public entry point
# ---------------------------------------------------------------------------


def notify(title, message, channels=None, kind="notify", job_id=None,
           conv_id=None, failed=False, actions=None):
    """Deliver one notification across every resolved channel.

    Never raises. Returns the notification record, with a `delivered` list
    of the channels that actually worked and `failed_channels` for the ones
    that didn't — visible in `jarvis sched-tick` output, because a toast
    that silently isn't appearing is otherwise very hard to diagnose.
    """
    config = _load_config()
    record = {
        "id": secrets.token_hex(6),
        "title": (title or "Jarvis").strip()[:200],
        "message": (message or "").strip()[:MAX_MESSAGE_CHARS],
        "kind": kind or "notify",
        "job_id": job_id,
        "conv_id": conv_id,
        "failed": bool(failed),
        "actions": list(actions or []),
        "created_at": datetime.now().replace(microsecond=0).isoformat(),
        "seen_by": [],
        "delivered": [],
        "failed_channels": [],
    }

    if not config.get("enabled", True):
        record["delivered"] = []
        record["failed_channels"] = ["disabled"]
        return record

    wanted = channels or config.get("channels", {}).get(record["kind"]) or ["inbox", "stream"]
    wanted = [c for c in wanted if c in CHANNELS]
    if "inbox" not in wanted:
        # Always durable. A caller opting out of every channel would
        # otherwise create a notification that exists nowhere — the failure
        # this module's docstring exists to prevent.
        wanted = ["inbox"] + wanted

    for channel in dict.fromkeys(wanted):
        try:
            ok = _DELIVERERS[channel](record, config)
        except Exception:  # noqa: BLE001 — a broken channel is not fatal
            ok = False
        (record["delivered"] if ok else record["failed_channels"]).append(channel)

    return record


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def _deliver_inbox(record, config):
    items = _load_inbox()
    items.append(record)
    return _save_inbox(items)


def _deliver_stream(record, config):
    """Emit a JARVIS_MEDIA line so a web console watching this process's
    stderr renders the notification immediately.

    Same envelope every other producer uses (see present_tools.py /
    dev_agent_events.py): three tab-separated fields, with the whole payload
    as JSON in the third — app.js's existing line.split("\\t") dispatch
    picks it up with one new branch and no change to the convention.
    """
    # seen_by/delivered/failed_channels are deliberately excluded: this runs
    # *during* the delivery loop, so those three are a half-filled snapshot
    # that would be actively misleading in the browser ("delivered: inbox"
    # on a notification that also toasted). The UI doesn't need them.
    skip = ("seen_by", "delivered", "failed_channels")
    try:
        line = "JARVIS_MEDIA\tnotification\t" + json.dumps(
            {k: v for k, v in record.items() if k not in skip},
            default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        return False
    return True


def _deliver_toast(record, config):
    """Native OS notification. Best-effort by design — there is no portable
    way to do this, so each platform gets its own attempt and a failure just
    means the inbox carries the notification instead."""
    text = record["message"].replace("\r", " ").replace("\n", " ")[:250]
    title = record["title"][:64]
    if sys.platform.startswith("win"):
        return _toast_windows(title, text, config)
    if sys.platform == "darwin":
        return _toast_macos(title, text)
    return _toast_linux(title, text, record)


def _toast_windows(title, text, config):
    # BurntToast produces a real Windows 10/11 action-centre toast, but it's
    # a third-party module that may not be installed. The NotifyIcon
    # fallback below needs nothing beyond stock .NET, so there's always a
    # path that works.
    if config.get("prefer_burnt_toast", True):
        script = (
            "if (Get-Module -ListAvailable -Name BurntToast) {"
            " Import-Module BurntToast;"
            " New-BurntToastNotification -Text %s,%s; exit 0 } else { exit 3 }"
            % (_ps_quote(title), _ps_quote(text))
        )
        if _run_powershell(script):
            return True
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = %s;"
        "$n.BalloonTipText = %s;"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(10000);"
        "Start-Sleep -Seconds 6;"
        "$n.Dispose()" % (_ps_quote(title), _ps_quote(text))
    )
    return _run_powershell(script)


def _ps_quote(text):
    """Single-quoted PowerShell literal — the only quoting style where the
    sole escape needed is doubling the quote itself, so a notification
    containing $, backticks, or quotes can't turn into an injected command."""
    return "'" + str(text).replace("'", "''") + "'"


def _run_powershell(script):
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=TOAST_TIMEOUT, creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _toast_macos(title, text):
    exe = shutil.which("osascript")
    if not exe:
        return False
    script = 'display notification %s with title %s' % (
        json.dumps(text), json.dumps(title))
    try:
        return subprocess.run([exe, "-e", script], capture_output=True,
                              timeout=TOAST_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _toast_linux(title, text, record):
    exe = shutil.which("notify-send")
    if not exe:
        return False
    urgency = "critical" if record.get("failed") else "normal"
    try:
        return subprocess.run([exe, "-u", urgency, "-a", "Jarvis", title, text],
                              capture_output=True, timeout=TOAST_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _deliver_voice(record, config):
    """Speak it. Off by default (voice_enabled) — a scheduled job talking out
    loud in a quiet room is a genuinely unwelcome surprise if nobody asked
    for it, so this one channel opts in rather than out."""
    if not config.get("voice_enabled"):
        return False
    try:
        from .voice import config as voice_config, tts as voice_tts
    except ImportError:
        return False
    if not voice_config.voice_enabled():
        return False
    spoken = "%s. %s" % (record["title"], record["message"])
    try:
        voice_tts.speak(spoken[:500])
        return True
    except Exception:  # noqa: BLE001 — TTS backends fail in many ways
        return False


def _deliver_playnite(record, config):
    """Reuse the existing playnite_notify tool rather than reimplementing its
    HTTP call — if the bridge's API changes, this follows automatically."""
    try:
        from . import playnite_api_tools
    except ImportError:
        return False
    try:
        result = playnite_api_tools.tool_playnite_notify({
            "text": "%s: %s" % (record["title"], record["message"][:180]),
            "type": "Error" if record.get("failed") else "Info",
        })
    except Exception:  # noqa: BLE001
        return False
    return not (isinstance(result, dict) and result.get("error"))


def _deliver_chat(record, config, platform):
    """Deliver one notification as a DM to the owner on a chat platform.

    Best-effort in the strongest sense: Instagram can only be reached
    inside a 24-hour window the owner opens themselves, and Discord needs
    a shared server. Both refuse as a matter of normal operation, not as
    an error — which is exactly why the inbox channel is always written
    too (see this module's docstring). Returning False here marks the
    channel as failed in `failed_channels` and changes nothing else.
    """
    try:
        from .channels import outbound
    except ImportError:
        return False
    title = (record.get("title") or "Jarvis").strip()
    body = (record.get("message") or "").strip()
    text = f"**{title}**\n{body}" if platform == "discord" else f"{title}\n{body}"
    ok, _detail = outbound.dm_owner(platform, text.strip())
    return bool(ok)


def _deliver_discord(record, config):
    return _deliver_chat(record, config, "discord")


def _deliver_instagram(record, config):
    return _deliver_chat(record, config, "instagram")


_DELIVERERS = {
    "inbox": _deliver_inbox,
    "stream": _deliver_stream,
    "toast": _deliver_toast,
    "voice": _deliver_voice,
    "playnite": _deliver_playnite,
    "discord": _deliver_discord,
    "instagram": _deliver_instagram,
}


# ---------------------------------------------------------------------------
# CLI-side rendering
# ---------------------------------------------------------------------------


def render_for_terminal(items):
    """Format pending notifications for a plain terminal. Returns "" when
    there's nothing, so callers can `if text: print(text)` without having to
    special-case an empty run."""
    if not items:
        return ""
    lines = []
    for item in items:
        mark = "!" if item.get("failed") else "*"
        head = "%s %s" % (mark, item.get("title") or "Jarvis")
        when = (item.get("created_at") or "")[11:16]
        if when:
            head += "  (%s)" % when
        lines.append(head)
        for line in (item.get("message") or "").splitlines():
            lines.append("  " + line)
    return "\n".join(lines)


def drain_for_cli(limit=10):
    """Fetch and immediately acknowledge everything waiting for a terminal
    session. One call, because every CLI caller wants both halves and a
    fetch without an ack would reprint the same reminder on every command
    until the end of time."""
    items = pending(consumer="cli", limit=limit)
    if items:
        acknowledge([i.get("id") for i in items], consumer="cli")
    return items
