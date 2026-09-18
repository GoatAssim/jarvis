"""Things Jarvis notices on its own.

THE IDEA
--------
Every check here answers a question nobody is going to think to ask until
it is already a problem: is the disk nearly full, did a daemon die, has an
API key started failing, is something in the backlog quietly rotting. The
information was always available — `jarvis doctor`, `jarvis daemons`,
`get_disk` — but only to someone who went and looked.

So this runs on the scheduler's tick, compares what it finds against what
it found last time, and pings the owner only when something has actually
changed for the worse.

ONLY-ON-CHANGE IS THE WHOLE DESIGN
----------------------------------
A monitor that reports "disk at 92%" every fifteen minutes trains you to
ignore it, and then it is worse than nothing — you have added noise AND
lost the signal. So every observation is compared against the last stored
one and a notification only fires when:

  * the condition is newly true (it was fine, now it isn't), or
  * it has materially worsened since the last alert (crossed the next
    threshold), or
  * it recovered, which is worth exactly one line.

State lives in ~/.jarvis/ambient_state.json. A cooldown per check is the
backstop for something that flaps across a threshold.

NOTHING HERE EXECUTES ANYTHING
------------------------------
Every check is read-only: stat a disk, read a status file, count a list.
An ambient monitor that can act is a monitor that can act wrongly at 3am
with nobody watching, and the payoff (it fixed it for you!) is not worth
the failure mode (it "fixed" it for you). Checks observe; the owner
decides.
"""

import shutil
import time
from pathlib import Path

from . import atomic_io

JARVIS_DIR = Path.home() / ".jarvis"
STATE_FILE = JARVIS_DIR / "ambient_state.json"
CONFIG_FILE = JARVIS_DIR / "ambient.json"

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ALERT = "alert"

DEFAULT_CONFIG = {
    "enabled": True,
    # Per-check switches. Off by name so a noisy one can be silenced
    # without losing the rest.
    "checks": {
        "disk": True,
        "daemons": True,
        "backlog": True,
        "logs": True,
    },
    # Don't re-alert about the same thing inside this window even if it
    # crosses another threshold.
    "cooldown_minutes": 180,
    "disk_warn_percent": 85,
    "disk_alert_percent": 93,
    # An item sitting in "doing" this long is almost certainly forgotten
    # rather than in progress.
    "stale_doing_days": 14,
    # Deliver via notify_owner (a DM) as well as the local inbox.
    "notify_owner": True,
}


def load_config():
    stored = atomic_io.read_json(CONFIG_FILE, default={}, expect=dict)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in stored.items() if k != "checks"})
    checks = dict(DEFAULT_CONFIG["checks"])
    if isinstance(stored.get("checks"), dict):
        checks.update(stored["checks"])
    cfg["checks"] = checks
    return cfg


def _load_state():
    return atomic_io.read_json(STATE_FILE, default={}, expect=dict)


def _save_state(state):
    atomic_io.write_json(STATE_FILE, state)


class Observation:
    """One thing noticed. `key` identifies the CONDITION, not the moment —
    two observations with the same key are the same ongoing situation, which
    is what makes change detection possible at all."""

    __slots__ = ("key", "severity", "message", "detail", "value")

    def __init__(self, key, severity, message, detail="", value=None):
        self.key = key
        self.severity = severity
        self.message = message
        self.detail = detail
        self.value = value

    def as_dict(self):
        return {"key": self.key, "severity": self.severity,
                "message": self.message, "detail": self.detail,
                "value": self.value}


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_disk(cfg):
    """Free space on the drive Jarvis itself lives on."""
    out = []
    try:
        usage = shutil.disk_usage(str(Path.home()))
    except OSError:
        return out
    used_pct = (usage.used / usage.total * 100) if usage.total else 0
    free_gb = usage.free / (1024 ** 3)
    if used_pct >= cfg["disk_alert_percent"]:
        out.append(Observation(
            "disk.full", SEVERITY_ALERT,
            f"Disk is {used_pct:.0f}% full — {free_gb:.1f} GB left.",
            "Downloads, logs and conversation history all write here. "
            "`jarvis doctor` shows what Jarvis's own directory is using.",
            value=int(used_pct)))
    elif used_pct >= cfg["disk_warn_percent"]:
        out.append(Observation(
            "disk.full", SEVERITY_WARN,
            f"Disk is {used_pct:.0f}% full — {free_gb:.1f} GB left.",
            value=int(used_pct)))
    return out


def check_daemons(cfg):
    """Daemons that died without being asked to stop."""
    out = []
    try:
        from . import daemons
    except Exception:  # noqa: BLE001
        return out
    for entry in daemons.list_daemons():
        if not entry.get("enabled", True):
            continue
        if entry.get("status") == daemons.STATUS_CRASHED:
            detail = entry.get("last_error") or ""
            out.append(Observation(
                f"daemon.crashed.{entry['id']}", SEVERITY_ALERT,
                f"The {entry.get('name') or entry['id']} daemon stopped "
                f"unexpectedly.",
                detail + f"  Restart with: jarvis daemon-start {entry['id']}",
                value=entry.get("exit_code")))
    return out


def check_backlog(cfg):
    """Work that has quietly stopped moving."""
    out = []
    try:
        from . import backlog
    except Exception:  # noqa: BLE001
        return out
    data = backlog.summary()
    stale_days = cfg.get("stale_doing_days", 14)
    stale = [i for i in data.get("stale_doing", []) if i.get("days", 0) >= stale_days]
    if stale:
        titles = ", ".join(i["title"][:40] for i in stale[:3])
        out.append(Observation(
            "backlog.stale", SEVERITY_INFO,
            f"{len(stale)} backlog item(s) have been 'doing' for over "
            f"{stale_days} days: {titles}",
            "Move them back to todo, or close them.",
            value=len(stale)))
    blocked = data.get("blocked") or []
    if len(blocked) >= 3:
        out.append(Observation(
            "backlog.blocked", SEVERITY_INFO,
            f"{len(blocked)} backlog items are blocked.",
            "; ".join(f"{b['title'][:30]} (waiting on {b['blocked_on'] or '?'})"
                      for b in blocked[:3]),
            value=len(blocked)))
    return out


def check_logs(cfg):
    """A burst of errors since the last look.

    Uses the raw file search rather than parsed conversation entries,
    because a daemon crash loop writes to a console log that
    logs.search() cannot see at all (see log_files.py).
    """
    out = []
    try:
        from . import log_files
    except Exception:  # noqa: BLE001
        return out
    try:
        found = log_files.search("traceback", mode="phrase", limit=50,
                                 sets=["daemons"])
    except Exception:  # noqa: BLE001
        return out
    count = len(found.get("results") or [])
    if count >= 5:
        files = sorted({r["file"] for r in found["results"]})
        out.append(Observation(
            "logs.errors", SEVERITY_WARN,
            f"{count} tracebacks in daemon logs.",
            "In: " + ", ".join(files[:3]),
            value=count))
    return out


_CHECKS = {
    "disk": check_disk,
    "daemons": check_daemons,
    "backlog": check_backlog,
    "logs": check_logs,
}


# ---------------------------------------------------------------------------
# change detection
# ---------------------------------------------------------------------------

def observe(cfg=None):
    """Run every enabled check. Pure — reports, notifies nothing."""
    cfg = cfg or load_config()
    found = []
    for name, fn in _CHECKS.items():
        if not cfg["checks"].get(name, True):
            continue
        try:
            found.extend(fn(cfg))
        except Exception:  # noqa: BLE001 — one broken check must not stop
            # the others; a monitor that dies silently is worse than one
            # that misses a single reading.
            continue
    return found


_SEVERITY_RANK = {SEVERITY_INFO: 0, SEVERITY_WARN: 1, SEVERITY_ALERT: 2}


def _worth_reporting(obs, previous, cooldown_seconds, now):
    """Has this genuinely changed since last time?"""
    if not previous:
        return True
    if _SEVERITY_RANK.get(obs.severity, 0) > _SEVERITY_RANK.get(previous.get("severity"), 0):
        # Escalation always reports, cooldown or not — going from warn to
        # alert is exactly the moment worth interrupting someone for.
        return True
    if now - (previous.get("reported_at") or 0) < cooldown_seconds:
        return False
    # Same severity, cooldown expired: only speak again if the number moved
    # meaningfully, so a disk hovering at 86% doesn't report hourly.
    old_value, new_value = previous.get("value"), obs.value
    if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
        return abs(new_value - old_value) >= max(2, abs(old_value) * 0.1)
    return False


def tick(cfg=None, now=None, notify=True):
    """Observe, diff against last time, and notify about what changed.

    Returns {"new": [...], "recovered": [...], "notified": n}. Called from
    the scheduler's tick so ambient monitoring runs on the timer that
    already exists rather than needing its own daemon.
    """
    cfg = cfg or load_config()
    now = time.time() if now is None else now
    if not cfg.get("enabled", True):
        return {"new": [], "recovered": [], "notified": 0, "skipped": "disabled"}

    cooldown = max(0, int(cfg.get("cooldown_minutes", 180))) * 60
    state = _load_state()
    seen = state.get("conditions") if isinstance(state.get("conditions"), dict) else {}

    current = observe(cfg)
    current_keys = {o.key for o in current}

    reportable = [o for o in current
                  if _worth_reporting(o, seen.get(o.key), cooldown, now)]

    # Recovery: something we alerted on is simply absent this time round.
    # Only ever mentioned for warn/alert — nobody needs "your backlog is no
    # longer mildly untidy".
    recovered = [
        {"key": key, "message": info.get("message") or key}
        for key, info in seen.items()
        if key not in current_keys
        and _SEVERITY_RANK.get(info.get("severity"), 0) >= 1
    ]

    new_state = {}
    for obs in current:
        previous = seen.get(obs.key) or {}
        entry = obs.as_dict()
        entry["first_seen"] = previous.get("first_seen") or now
        entry["reported_at"] = (now if obs in reportable
                                else previous.get("reported_at") or 0)
        new_state[obs.key] = entry
    _save_state({"conditions": new_state, "last_tick": now})

    notified = 0
    if notify and (reportable or recovered):
        notified = _deliver(cfg, reportable, recovered)

    return {
        "new": [o.as_dict() for o in reportable],
        "recovered": recovered,
        "notified": notified,
        "observed": len(current),
    }


def _deliver(cfg, reportable, recovered):
    """Send to the local inbox, and optionally DM the owner.

    Both are best-effort and wrapped: a monitoring system that can fail the
    tick it runs inside is a monitoring system that takes down the thing it
    was meant to watch.
    """
    lines = []
    for obs in reportable:
        prefix = {"alert": "!", "warn": "*", "info": "-"}.get(obs.severity, "-")
        lines.append(f"{prefix} {obs.message}")
        if obs.detail:
            lines.append(f"    {obs.detail}")
    for item in recovered:
        lines.append(f"+ Resolved: {item['message']}")
    if not lines:
        return 0
    body = "\n".join(lines)

    sent = 0
    try:
        from . import notifier
        notifier.notify("Jarvis noticed something", body, kind="ambient")
        sent += 1
    except Exception:  # noqa: BLE001
        pass

    worst = max((_SEVERITY_RANK.get(o.severity, 0) for o in reportable),
                default=0)
    # Only a real problem earns a phone buzz. Info-level observations stay
    # in the inbox and the digest.
    if cfg.get("notify_owner", True) and worst >= 1:
        try:
            from .channels import outbound
            outbound.notify_owner(body, platforms=None, first_success_only=True)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    return sent


def status():
    """What the monitor currently believes, for `jarvis ambient`."""
    state = _load_state()
    conditions = state.get("conditions") or {}
    return {
        "ok": True,
        "enabled": load_config().get("enabled", True),
        "last_tick": state.get("last_tick"),
        "active": sorted(conditions.values(),
                         key=lambda c: -_SEVERITY_RANK.get(c.get("severity"), 0)),
        "checks": list(_CHECKS),
    }
