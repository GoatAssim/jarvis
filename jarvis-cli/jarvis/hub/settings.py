"""Hub settings: defaults and the one validator (plan section 13).

Stored in hub.db `settings` (one JSON value per top-level key), read as
defaults + overrides.  `validate_update` is the ONLY place a write is checked, so
`dl_settings`, the panel and a future CLI cannot drift apart.

Only a subset is writable today.  The rest of the plan's settings (retention, Stremio,
per-provider blocks) already have defaults so later phases read them without a migration,
but nothing can change them until the phase that owns them adds the key to `WRITABLE`.

Two tiers of writable keys:
  * WRITABLE        everything `dl_settings` (confirm-gated) may change;
  * PREFS_WRITABLE  the everyday subset `dl_prefs` (not confirm-gated) may change: speed limit,
                    schedule, connections, per-type folders, notifications, open-when-done.
                    Left out on purpose: `library_root` and `allow_private_hosts`, which decide
                    where files are written and which private hosts the hub may fetch from.
"""

import copy
import datetime
from pathlib import Path

from . import naming, paths

DEFAULTS = {
    "library_root": "",          # "" = paths.default_library_root()
    "concurrency": {"total": 4, "http": 3, "ytdlp": 2, "stremio": 3},
    "rate_limit_kbps": 0,        # 0 = unlimited; shared across running hub downloads
    # Speed windows: [{"days": "all", "from": "01:00", "to": "07:00", "rate_limit_kbps": 0}].
    # Inside a window its limit replaces `rate_limit_kbps` (0 there = unlimited); first match wins.
    "schedule": [],
    "retention": {"keep_history_days": 90, "auto_delete_files": False},
    "allow_private_hosts": [],   # the SSRF allow-list ("host" or "host:port")
    "http": {"stall_seconds": 60, "max_retries": 3, "connect_timeout": 15},
    "stremio": {"mode": "save", "stall_seconds": 90, "max_retries": 3},   # phase 4
    "providers": {},             # phase 5: per-provider blocks
    "provider_limits": {"max_rows": 500, "max_report_kb": 64, "max_reports_per_s": 20,
                        "stale_after_s": 30},
    "allowed_link_schemes": ["steam", "https"],
    # Where finished files go, inside the library root, by kind of file.
    "folders": {"video": "Video", "audio": "Audio", "file": "Files"},
    # What the hub does the moment a download finishes.  off | reveal (show the file in its
    # folder) | folder (open the folder) | open (open the file; never a program or script).
    # A single download can override it (item options `open`).
    "after_download": {"open": "off"},
    # Notifications.  Delivery is the existing notifier (jarvis/notifier.py): the inbox, the
    # in-app toast, the desktop notification, voice and the phone DM, chosen by the notifier's own
    # importance LEVEL (1 silent .. 5 confirm) and positions in Settings > Notifications /
    # notify_config.json.  The hub only decides WHICH events fire.  `level` None = follow the
    # notifier's own default for kind "download"; 1-5 overrides it for that event.
    # `min_seconds`: a finished download that took less is not announced.  `coalesce_s`:
    # finishes within this many seconds fold into one message.  `providers`: also announce
    # tool-supplied rows (Steam, ...).
    "notify": {
        "enabled": True, "min_seconds": 0, "coalesce_s": 8, "providers": True,
        "started": {"enabled": False, "level": None},
        "done": {"enabled": True, "level": None},
        "failed": {"enabled": True, "level": None},
        "stalled": {"enabled": False, "level": None},
        "group_done": {"enabled": True, "level": None},
        "queue_empty": {"enabled": True, "level": None},
    },
    "limits": {"max_items_per_call": 50, "confirm_above_items": 5, "max_bytes_per_call_gb": 150},
    "demo": {"enabled": False, "started_at": 0},
}

# key -> checker(value) -> (ok_value, error).  Nested keys are written as "a.b".
EVENTS = ("started", "done", "failed", "stalled", "group_done", "queue_empty")
OPEN_MODES = ("off", "reveal", "folder", "open")
FOLDER_KINDS = ("video", "audio", "file")
MAX_WINDOWS = 10

PREFS_WRITABLE = {
    "rate_limit_kbps", "schedule",
    "concurrency.total", "concurrency.http", "concurrency.ytdlp", "concurrency.stremio",
    "http.stall_seconds", "http.max_retries",
    "after_download.open",
    "notify.enabled", "notify.min_seconds", "notify.coalesce_s", "notify.providers",
} | {"folders." + k for k in FOLDER_KINDS} \
  | {"notify.%s.%s" % (e, f) for e in EVENTS for f in ("enabled", "level")}

WRITABLE = PREFS_WRITABLE | {"library_root", "allow_private_hosts"}


def _deep_merge(base, over):
    """`over` laid onto `base`, dict into dict all the way down (lists and scalars replace)."""
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def merged(stored):
    """Defaults with the stored overrides laid over them (dicts merge, so an event the
    owner never touched keeps its default)."""
    out = copy.deepcopy(DEFAULTS)
    for key, value in (stored or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def library_root(settings):
    raw = (settings.get("library_root") or "").strip()
    return Path(raw).expanduser() if raw else paths.default_library_root()


def _int_in(value, lo, hi, label):
    if isinstance(value, bool):
        return None, "%s must be a whole number" % label
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None, "%s must be a whole number" % label
    if num < lo or num > hi:
        return None, "%s must be between %d and %d" % (label, lo, hi)
    return num, None


def _check_root(value):
    text = str(value or "").strip()
    if not text:
        return "", None                      # back to the default
    if "\x00" in text:
        return None, "library_root has an invalid character"
    path = Path(text).expanduser()
    if not path.is_absolute():
        return None, "library_root must be an absolute path"
    if len(path.parts) <= 1:
        return None, "library_root can't be a drive or filesystem root"
    return str(path), None


def _check_hosts(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None, "allow_private_hosts must be a list of host or host:port entries"
    out = []
    for raw in value[:50]:
        text = str(raw).strip().lower()
        if not text or len(text) > 255 or any(c in text for c in " /\\@?#\x00"):
            return None, "bad allow_private_hosts entry: %r" % str(raw)[:60]
        out.append(text)
    return out, None


def _bool(label):
    def check(value):
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True, None
        if text in ("false", "0", "no", "off"):
            return False, None
        return None, "%s must be true or false" % label
    return check


def _check_level(value):
    """A notifier importance level 1-5, or None / "default" to follow the notifier's own."""
    if value is None or str(value).strip().lower() in ("", "default", "auto", "none"):
        return None, None
    return _int_in(value, 1, 5, "level")


def _check_open_mode(value):
    text = str(value if value is not None else "").strip().lower()
    if text in ("none", "nothing", "no", "false", ""):
        text = "off"
    if text not in OPEN_MODES:
        return None, "after_download.open must be one of: %s" % ", ".join(OPEN_MODES)
    return text, None


def _check_kind_folder(label):
    def check(value):
        cleaned = naming.clean_relative(value)
        if cleaned is None:
            return None, ("%s must be a plain sub-folder name like Movies or Media/Video: "
                          "not a full path, a drive, or '..'" % label)
        return cleaned, None
    return check


_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_SETS = {"all": _DAY_NAMES, "every day": _DAY_NAMES, "daily": _DAY_NAMES,
             "weekdays": _DAY_NAMES[:5], "weekend": _DAY_NAMES[5:], "weekends": _DAY_NAMES[5:]}


def _clock(text):
    """'07:30' -> minutes after midnight, or None."""
    parts = str(text or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    hh, mm = int(parts[0]), int(parts[1])
    if hh > 23 or mm > 59 or len(parts[1]) != 2:
        return None
    return hh * 60 + mm


def _check_schedule(value):
    if value in (None, ""):
        return [], None
    if not isinstance(value, (list, tuple)):
        return None, "schedule must be a list of windows"
    out = []
    for n, raw in enumerate(value[:MAX_WINDOWS], 1):
        if not isinstance(raw, dict):
            return None, "schedule window %d must be an object" % n
        days = raw.get("days", "all")
        if isinstance(days, str):
            names = _DAY_SETS.get(days.strip().lower())
            if names is None:
                names = tuple(d.strip()[:3].lower() for d in days.split(",") if d.strip())
        elif isinstance(days, (list, tuple)):
            names = tuple(str(d).strip()[:3].lower() for d in days)
        else:
            names = ()
        if not names or any(d not in _DAY_NAMES for d in names):
            return None, "schedule window %d: days must be all, weekdays, weekends or a list like mon,tue" % n
        start, end = _clock(raw.get("from")), _clock(raw.get("to"))
        if start is None or end is None:
            return None, "schedule window %d: from and to must be times like 01:00" % n
        if start == end:
            return None, "schedule window %d: from and to can't be the same time" % n
        rate, err = _int_in(raw.get("rate_limit_kbps", 0), 0, 10_000_000, "schedule window %d rate_limit_kbps" % n)
        if err:
            return None, err
        ordered = [d for d in _DAY_NAMES if d in names]
        out.append({"days": ordered, "from": "%02d:%02d" % divmod(start, 60), "to": "%02d:%02d" % divmod(end, 60),
                    "rate_limit_kbps": rate})
    if len(value) > MAX_WINDOWS:
        return None, "at most %d schedule windows" % MAX_WINDOWS
    return out, None


_CHECKS = {
    "library_root": _check_root,
    "rate_limit_kbps": lambda v: _int_in(v, 0, 10_000_000, "rate_limit_kbps"),
    "allow_private_hosts": _check_hosts,
    "concurrency.total": lambda v: _int_in(v, 1, 16, "concurrency.total"),
    "concurrency.http": lambda v: _int_in(v, 1, 16, "concurrency.http"),
    "concurrency.ytdlp": lambda v: _int_in(v, 1, 16, "concurrency.ytdlp"),
    "concurrency.stremio": lambda v: _int_in(v, 1, 16, "concurrency.stremio"),
    "http.stall_seconds": lambda v: _int_in(v, 10, 3600, "http.stall_seconds"),
    "http.max_retries": lambda v: _int_in(v, 0, 10, "http.max_retries"),
    "schedule": _check_schedule,
    "after_download.open": _check_open_mode,
    "notify.enabled": _bool("notify.enabled"),
    "notify.providers": _bool("notify.providers"),
    "notify.min_seconds": lambda v: _int_in(v, 0, 86400, "notify.min_seconds"),
    "notify.coalesce_s": lambda v: _int_in(v, 0, 600, "notify.coalesce_s"),
}
for _kind in FOLDER_KINDS:
    _CHECKS["folders." + _kind] = _check_kind_folder("folders." + _kind)
for _event in EVENTS:
    _CHECKS["notify.%s.enabled" % _event] = _bool("notify.%s.enabled" % _event)
    _CHECKS["notify.%s.level" % _event] = _check_level


def validate_update(updates, allowed=None):
    """{"concurrency.http": 2, "library_root": "D:/x"} -> (clean, errors).

    Unknown or not-yet-writable keys are errors, never silently ignored: a typo in a
    setting name must not look like success.  `allowed` narrows the keys (dl_prefs passes
    PREFS_WRITABLE); the default is everything in WRITABLE.
    """
    allowed = WRITABLE if allowed is None else allowed
    clean, errors = {}, []
    if not isinstance(updates, dict) or not updates:
        return clean, ["nothing to change"]
    for key, value in updates.items():
        if key not in allowed:
            if key in WRITABLE:
                errors.append("%r can't be changed with dl_prefs; it needs dl_settings (which asks you first)" % str(key)[:60])
            else:
                errors.append("%r can't be changed here" % str(key)[:60])
            continue
        checked, err = _CHECKS[key](value)
        if err:
            errors.append(err)
        else:
            clean[key] = checked
    return clean, errors


def apply_dotted(stored, clean):
    """Fold {"a.b": v} into the stored top-level dict and return the keys that changed."""
    changed = []
    for dotted, value in clean.items():
        head, _, tail = dotted.partition(".")
        if tail:
            block = copy.deepcopy(stored.get(head)) if isinstance(stored.get(head), dict) else {}
            node = block
            *middle, leaf = tail.split(".")
            for part in middle:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[leaf] = value
            stored[head] = block
        else:
            stored[head] = value
        changed.append(head)
    return sorted(set(changed))


# --------------------------------------------------------------------------- speed schedule

def _minutes(now):
    return now.hour * 60 + now.minute


def scheduled_window(settings, now=None):
    """The schedule window that applies right now, or None.  First match wins.

    A window whose `to` is earlier than its `from` runs overnight: its days are the days it
    STARTS on (Fri 22:00 -> 06:00 is a Friday window that is still active early Saturday).
    """
    now = now or datetime.datetime.now()
    today = _DAY_NAMES[now.weekday()]
    yesterday = _DAY_NAMES[(now.weekday() - 1) % 7]
    minute = _minutes(now)
    for window in settings.get("schedule") or []:
        try:
            start, end = _clock(window["from"]), _clock(window["to"])
            days = window.get("days") or []
        except (KeyError, TypeError, AttributeError):
            continue
        if start is None or end is None:
            continue
        if start < end:
            if today in days and start <= minute < end:
                return window
        else:                               # overnight
            if (today in days and minute >= start) or (yesterday in days and minute < end):
                return window
    return None


def effective_rate_limit_kbps(settings, now=None):
    """(kbps, source): the global limit in force now.  0 = unlimited.
    `source` is "schedule" inside a window, else "manual" (or "none" when unlimited)."""
    window = scheduled_window(settings, now)
    if window is not None:
        kbps = int(window.get("rate_limit_kbps") or 0)
        return kbps, "schedule"
    kbps = int(settings.get("rate_limit_kbps") or 0)
    return kbps, ("manual" if kbps > 0 else "none")


def item_limit_kbps(item):
    """A single download's own cap from its options, in KB/s (0 = no cap of its own)."""
    try:
        value = int(((item or {}).get("options") or {}).get("limit_kbps") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)
