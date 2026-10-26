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
                    `ext_folders` is in the everyday tier only for sub-folders of the library
                    root; a full path in it (a folder outside the library) needs `dl_settings`.
"""

import copy
import datetime
import re
from pathlib import Path

from . import naming, paths

DEFAULTS = {
    "library_root": "",          # "" = paths.default_library_root()
    "concurrency": {"total": 4, "http": 3, "ytdlp": 2, "stremio": 3, "qbittorrent": 2},
    "rate_limit_kbps": 0,        # 0 = unlimited; shared across running hub downloads
    # Speed windows: [{"days": "all", "from": "01:00", "to": "07:00", "rate_limit_kbps": 0}].
    # Inside a window its limit replaces `rate_limit_kbps` (0 there = unlimited); first match wins.
    "schedule": [],
    # keep_history_days: the daemon deletes finished history ROWS (never files) older than this;
    # 0 or less = keep forever.  auto_delete_files exists but nothing reads it: the hub never
    # deletes a file by itself.
    "retention": {"keep_history_days": 90, "auto_delete_files": False},
    # A provider row that vanished from its provider's list sits in `unknown`; after this many hours
    # (counted from the moment it became unknown) it settles into `cancelled`.  A row flagged stale
    # (the provider itself is not answering, e.g. Steam closed) is never settled.
    "unknown_settle_hours": 24,
    "allow_private_hosts": [],   # the SSRF allow-list ("host" or "host:port")
    # segments: parallel connections per file (1 = off, the default); only used for a file at least
    # segment_min_bytes big whose server supports Range.  A download can override it (options.segments).
    "http": {"stall_seconds": 60, "max_retries": 3, "connect_timeout": 15,
             "segments": 1, "segment_min_bytes": 32 * 1024 * 1024},
    "stremio": {"mode": "save", "stall_seconds": 90, "max_retries": 3, "start_wait_seconds": 60,   # phase 4 
    "metadata_timeout": 90},      # seconds to wait for a torrent's file list (no peers = no list)
    # qBittorrent engine (brief 31, decision 11).  Only its BEHAVIOUR lives here; the connection (address,
    # user name, PASSWORD) and the on/off switch stay in ~/.jarvis/qbittorrent.json, so no settings answer
    # can ever show a secret.  A key the file also sets is the file's (see engines/qbittorrent.py).
    "qbittorrent": {"prefer_for_magnets": False, "cancel_deletes_data": True, "after_done": "keep",
                    "save_subfolder": "Torrents", "category": "", "tag": "jarvis",
                    "metadata_timeout_s": 900, "stall_minutes": 0, "poll_s": 2.0},
    "providers": {},             # phase 5: per-provider blocks
    "provider_limits": {"max_rows": 500, "max_report_kb": 64, "max_reports_per_s": 20,
                        "stale_after_s": 30},
    "allowed_link_schemes": ["steam", "https"],
    # Where finished files go, inside the library root, by kind of file.
    "folders": {"video": "Video", "audio": "Audio", "file": "Files"},
    # A folder per file extension, which wins over `folders`: {".iso": "ISOs", ".pdf": "D:/Docs"}.
    # A value is a sub-folder of the library root, or (dl_settings only) a full path.  A longer
    # extension wins over a shorter one (".tar.gz" over ".gz").  A single download's own folder
    # (item option `subdir` / `dest_abs`) wins over this.
    "ext_folders": {},
    # What the hub does the moment a download finishes.  off | reveal (show the file in its
    # folder) | folder (open the folder) | open (open the file; never a program or script).
    # A single download can override it (item options `open`).
    "after_download": {"open": "off"},
    # Notifications.  Delivery is the existing notifier (jarvis/notifier.py): the inbox, the
    # in-app toast, the desktop notification, voice and the phone DM, chosen by the notifier's own
    # importance LEVEL (silent .. confirm, whatever the notifier defines) and positions in Settings > Notifications /
    # notify_config.json.  The hub only decides WHICH events fire.  `level` None = follow the
    # notifier's own default for kind "download"; a level number overrides it for that event
    # (the valid numbers come from notifier.py, never from a list kept here).
    # `min_seconds`: a finished download that took less is not announced.  `coalesce_s`:
    # finishes within this many seconds fold into one message.  `providers`: also announce
    # tool-supplied rows (Steam, ...).
    "notify": {
        "enabled": True, "min_seconds": 0, "coalesce_s": 8, "providers": True,
        # one on/off switch per provider (brief 31): {"steam": True, "playnite": True}; a provider with
        # no entry is on.  `providers` above is the master switch for all of them.
        "by_provider": {"steam": True, "playnite": True},
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

# Settings the hub itself understands inside a PROVIDER's own settings block (the owner's choices,
# stored on the provider row and changed with `dl_provider_prefs`, never by the provider).  A key
# the hub doesn't know here (Steam's `root`, a tool's own) is passed through untouched, as before.
#   steam.notify_min_update_gb   a Steam UPDATE is announced only when it is at least this many GB
#                                (1 GB = 1024^3 bytes, the same unit the panel shows).  0 = every
#                                update.  Installs of new games are not subject to it: they follow
#                                the normal `notify.done` rules.  Missing from the stored block
#                                (every provider row made before this key existed) = the default.
STEAM_MIN_UPDATE_GB_DEFAULT = 1.0
MAX_MIN_UPDATE_GB = 10_000.0

# The providers that have a notification switch of their own out of the box.  Any other provider id
# is accepted too (`notify.by_provider.<id>`), see `notify_provider_key`.
NOTIFY_PROVIDERS = ("steam", "playnite")
QBITTORRENT_PREFS = ("prefer_for_magnets", "category", "metadata_timeout_s", "stall_minutes", "poll_s")
QBITTORRENT_CONFIRM = ("cancel_deletes_data", "after_done", "save_subfolder", "tag")
QBITTORRENT_AFTER_DONE = ("keep", "pause", "remove")

PREFS_WRITABLE = {
    "rate_limit_kbps", "schedule",
    "concurrency.total", "concurrency.http", "concurrency.ytdlp", "concurrency.stremio", "concurrency.qbittorrent",
    "http.stall_seconds", "http.max_retries", "http.segments", "http.segment_min_bytes",
    "stremio.stall_seconds", "stremio.max_retries", "stremio.metadata_timeout", "stremio.start_wait_seconds",
    "after_download.open",
    "notify.enabled", "notify.min_seconds", "notify.coalesce_s", "notify.providers",
    "ext_folders", "unknown_settle_hours",
} | {"folders." + k for k in FOLDER_KINDS} \
  | {"notify.%s.%s" % (e, f) for e in EVENTS for f in ("enabled", "level")} \
  | {"notify.by_provider.%s" % p for p in NOTIFY_PROVIDERS} \
  | {"qbittorrent." + k for k in QBITTORRENT_PREFS}

# Behaviour that deletes data or moves files, or changes which torrents count as the hub's own, goes
# through dl_settings (which asks first), like library_root.
WRITABLE = PREFS_WRITABLE | {"library_root", "allow_private_hosts"} \
    | {"qbittorrent." + k for k in QBITTORRENT_CONFIRM} \
    | {"retention.keep_history_days"}       # brief 7C: deletes History rows, so dl_settings (asks first), never dl_prefs


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


KEEP_HISTORY_MAX_DAYS = 3650


def _check_keep_history_days(value):
    """retention.keep_history_days: a whole number of days, 0 (keep everything) to KEEP_HISTORY_MAX_DAYS.
    1.5 is refused, not cut to 1 (`_int_in` alone would cut it); True and text like "ten" are refused too."""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or value != int(value):
            return None, "retention.keep_history_days must be a whole number"
        value = int(value)
    return _int_in(value, 0, KEEP_HISTORY_MAX_DAYS, "retention.keep_history_days")


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


def _notifier_levels():
    """(lowest, highest, {name: number}) as the notifier defines them right now.  Read live, so a
    level added to jarvis/notifier.py (a row in notify_registry.PRESETS) is accepted here with no
    change to the hub.  If the notifier can't be imported at all the bounds come from the
    registry module directly (plain data, no other imports); only if even that fails is a level
    refused."""
    try:
        from .. import notifier
        names = {str(n).lower(): int(k) for k, n in notifier.LEVEL_NAMES.items()}
        return int(notifier.MIN_LEVEL), int(notifier.MAX_LEVEL), names
    except Exception:  # noqa: BLE001 - the validator must keep working without the notifier
        try:
            from .. import notify_registry
            return (int(notify_registry.MIN_LEVEL), int(notify_registry.MAX_LEVEL),
                    {str(p["id"]).lower(): int(p["level"]) for p in notify_registry.PRESETS})
        except Exception:  # noqa: BLE001
            return 0, -1, {}


def _check_level(value):
    """A notifier importance level (by number or by its name), or None / "default" to follow the
    notifier's own."""
    if value is None or str(value).strip().lower() in ("", "default", "auto", "none"):
        return None, None
    low, high, names = _notifier_levels()
    named = names.get(str(value).strip().lower())
    if named is not None and low <= named <= high:
        return named, None
    return _int_in(value, low, high, "level")


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


MAX_EXT_FOLDERS = 50


def clean_ext(text):
    """'.ISO' / 'iso' -> '.iso'; '.tar.gz' stays; None if it isn't a plain extension."""
    raw = str(text or "").strip().lower()
    if raw and not raw.startswith("."):
        raw = "." + raw
    if not re.match(r"^\.[a-z0-9_+\-]{1,12}(\.[a-z0-9_+\-]{1,12}){0,2}$", raw):
        return None
    return raw


# Places a download may never be written into: the same list as actions/path_tools._protected_reason(path,
# writing=True).  The hub doesn't import the tool files, so the list is kept here and
# tests/test_hub_http_verify.py compares the two (a change in one that isn't made in the other fails there).
_POSIX_SYSTEM_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys")
_WIN_SYSTEM_DIRS = ("windows", "program files", "program files (x86)", "programdata")


def protected_folder_reason(path):
    """Why a download must never be written into this folder (a drive root, the home folder itself, an
    operating-system folder, `~/.jarvis`), or None.  Pure text: nothing is touched, no link is followed."""
    import os
    import sys

    def norm(p):
        return os.path.normcase(os.path.abspath(str(p)))

    def inside(child, parent):
        c, par = norm(child), norm(parent)
        return c == par or c.startswith(par.rstrip("\\/") + os.sep)

    p = Path(path)
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        home = None
    if home is not None and inside(p, home / ".jarvis"):
        return "that is Jarvis's own settings/state folder (~/.jarvis)"
    if p.parent == p:
        return "that is a drive/filesystem root"
    if home is not None and norm(p) == norm(home):
        return "that is your home folder itself"
    if sys.platform == "win32":
        parts = [x.lower() for x in p.parts]
        if len(parts) >= 2 and parts[1] in _WIN_SYSTEM_DIRS:
            return "that is inside a Windows system folder"
    else:
        for d in _POSIX_SYSTEM_DIRS:
            if inside(p, d):
                return "that is inside an operating-system folder"
    return None


def abs_folder(text):
    """A full-path folder as a normalised string, or None when it isn't one (or is unusable)."""
    raw = str(text or "").strip()
    if not raw or "\x00" in raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or len(path.parts) <= 1:
        return None                                  # relative, or a drive / filesystem root
    if ".." in path.parts:
        return None
    if protected_folder_reason(path):
        return None                                  # home itself, an OS folder, ~/.jarvis: never a download folder
    return str(path)


def is_absolute_spec(text):
    """Does this folder spec look like a full path (rather than a sub-folder of the library)?"""
    raw = str(text or "").strip().replace("\\", "/")
    return bool(raw) and (raw.startswith("/") or raw.startswith("~") or bool(re.match(r"^[A-Za-z]:", raw)))


def _check_ext_folders(value, allow_abs=False):
    if value in (None, ""):
        return {}, None
    if not isinstance(value, dict):
        return None, "ext_folders must be an object like {\".iso\": \"ISOs\", \".pdf\": \"Documents\"}"
    if len(value) > MAX_EXT_FOLDERS:
        return None, "at most %d extension folders" % MAX_EXT_FOLDERS
    out = {}
    for raw_ext, raw_folder in value.items():
        ext = clean_ext(raw_ext)
        if ext is None:
            return None, "bad extension %r: use something like .iso or .tar.gz" % str(raw_ext)[:30]
        if raw_folder in (None, ""):
            continue                                  # an empty folder removes that extension
        if is_absolute_spec(raw_folder):
            if not allow_abs:
                return None, ("%s: a full path like %s writes outside the library, so it needs "
                              "dl_settings (which asks you first)" % (ext, str(raw_folder)[:60]))
            full = abs_folder(raw_folder)
            if full is None:
                return None, "%s: %r isn't a usable full path (no drive roots, no '..')" % (ext, str(raw_folder)[:60])
            out[ext] = full
        else:
            cleaned = naming.clean_relative(raw_folder)
            if cleaned is None:
                return None, ("%s: the folder must be a plain sub-folder like Movies or Media/ISOs "
                              "(or a full path via dl_settings), not '..'" % ext)
            out[ext] = cleaned
    return out, None


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


def _check_gb(label):
    """A size in gigabytes: a finite number from 0 to MAX_MIN_UPDATE_GB, kept to 3 decimals."""
    def check(value):
        if isinstance(value, bool) or value is None:
            return None, "%s must be a number of GB (0 = every update)" % label
        try:
            num = float(str(value).strip())
        except (TypeError, ValueError):
            return None, "%s must be a number of GB (0 = every update)" % label
        if num != num or num in (float("inf"), float("-inf")):
            return None, "%s must be a finite number of GB" % label
        if num < 0 or num > MAX_MIN_UPDATE_GB:
            return None, "%s must be between 0 and %d GB" % (label, int(MAX_MIN_UPDATE_GB))
        return round(num, 3), None
    return check


# provider id -> {key: (default, checker)}
PROVIDER_SETTINGS = {
    "steam": {"notify_min_update_gb": (STEAM_MIN_UPDATE_GB_DEFAULT, _check_gb("notify_min_update_gb"))},
}


# N23e: the two settings of the update checks.  They belong to EVERY provider (not one, like Steam's
# threshold above), so they live apart from PROVIDER_SETTINGS: `owner_provider_settings` keeps returning
# exactly what it always did, and `update_settings` below is what the Updates side reads.
#   update_checks      True = the hub checks this provider for available updates (default), False = never
#   update_interval_h  hours between two automatic checks (default 24; 0.5 to 720)
UPDATE_CHECKS_DEFAULT = True
UPDATE_INTERVAL_H_DEFAULT = 24.0
UPDATE_INTERVAL_H_MIN = 0.5
UPDATE_INTERVAL_H_MAX = 720.0


def _check_hours(label):
    """A number of hours from UPDATE_INTERVAL_H_MIN to UPDATE_INTERVAL_H_MAX, kept to 3 decimals."""
    def check(value):
        message = "%s must be a number of hours from %g to %g" % (label, UPDATE_INTERVAL_H_MIN, UPDATE_INTERVAL_H_MAX)
        if isinstance(value, bool) or value is None:
            return None, message
        try:
            num = float(str(value).strip())
        except (TypeError, ValueError):
            return None, message
        if num != num or num in (float("inf"), float("-inf")):
            return None, message
        if num < UPDATE_INTERVAL_H_MIN or num > UPDATE_INTERVAL_H_MAX:
            return None, message
        return round(num, 3), None
    return check


UPDATE_SETTINGS = {
    "update_checks": (UPDATE_CHECKS_DEFAULT, _bool("update_checks")),
    "update_interval_h": (UPDATE_INTERVAL_H_DEFAULT, _check_hours("update_interval_h")),
}


def update_settings(stored, starting_interval_h=None):
    """The effective update settings of one provider: {"update_checks": bool, "update_interval_h": float}.

    A stored value that validates wins; then `starting_interval_h` (what the tool file declared as
    `update_interval_h`: a starting value the owner then owns), when it validates; then the default.  A
    stored value that no longer validates never stops a check or makes one run too often."""
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key, (default, check) in UPDATE_SETTINGS.items():
        value = None
        if key in stored:
            value, err = check(stored[key])
            if err:
                value = None
        if value is None and key == "update_interval_h" and starting_interval_h is not None:
            value, err = check(starting_interval_h)
            if err:
                value = None
        out[key] = default if value is None else value
    return out


def validate_provider_settings(provider_id, settings):
    """{\"notify_min_update_gb\": \"2.5\"} -> (clean, errors) for one provider's settings block.

    Keys the hub knows for THIS provider are range-checked and normalised.  A key that belongs to
    another provider is an error (a setting that silently does nothing looks like success).  Any
    other key is passed through unchanged, which keeps Steam's `root` and tool-defined keys working.
    `update_checks` and `update_interval_h` are known for every provider (UPDATE_SETTINGS).
    """
    known = PROVIDER_SETTINGS.get(str(provider_id or ""), {})
    foreign = {k: pid for pid, block in PROVIDER_SETTINGS.items() if pid != provider_id for k in block}
    clean, errors = {}, []
    for key, value in (settings or {}).items():
        if key in known:
            checked, err = known[key][1](value)
            if err:
                errors.append(err)
            else:
                clean[key] = checked
        elif key in UPDATE_SETTINGS:
            checked, err = UPDATE_SETTINGS[key][1](value)
            if err:
                errors.append(err)
            else:
                clean[key] = checked
        elif key in foreign:
            errors.append("%s is a %s setting; provider %r doesn't have it" % (key, foreign[key], str(provider_id)[:30]))
        else:
            clean[key] = value
    return clean, errors


def provider_setting(provider_id, stored, key):
    """The effective value of a hub-known provider setting: the stored one when it is valid, else
    the default.  A stored value that no longer validates never breaks a notification."""
    entry = PROVIDER_SETTINGS.get(str(provider_id or ""), {}).get(key)
    if entry is None:
        return None
    default, check = entry
    if isinstance(stored, dict) and key in stored:
        value, err = check(stored[key])
        if not err:
            return value
    return default


def owner_provider_settings(provider_id, stored):
    """The owner-facing settings the hub understands for a provider, with defaults filled in
    (what dl_providers and the Providers tab show).  Deliberately NOT the whole stored block."""
    return {key: provider_setting(provider_id, stored, key) for key in PROVIDER_SETTINGS.get(str(provider_id or ""), {})}


def steam_min_update_bytes(stored):
    """The Steam update-size threshold as bytes (0 = announce every update)."""
    return int(provider_setting("steam", stored, "notify_min_update_gb") * 1024 ** 3)


def _float_in(value, lo, hi, label):
    if isinstance(value, bool):
        return None, "%s must be a number" % label
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None, "%s must be a number" % label
    if num != num or num < lo or num > hi:
        return None, "%s must be between %g and %g" % (label, lo, hi)
    return num, None


def _check_qb_label(label, max_len):
    """A category or a tag as qBittorrent keeps it: plain text, no commas (tags are comma-separated), no control characters."""
    def check(value):
        if not isinstance(value, str):
            return None, "%s must be text" % label
        text = value.strip()
        if len(text) > max_len or any(ord(c) < 32 or c in ",\\" for c in text):
            return None, "%s must be at most %d characters, with no commas, backslashes or control characters" % (label, max_len)
        return text, None
    return check


def _check_qb_after_done(value):
    if value not in QBITTORRENT_AFTER_DONE:
        return None, "qbittorrent.after_done must be one of: %s" % ", ".join(QBITTORRENT_AFTER_DONE)
    return value, None


def notify_provider_key(key):
    """The provider id in a `notify.by_provider.<id>` key, or None (any other key, or an odd id)."""
    prefix = "notify.by_provider."
    if not isinstance(key, str) or not key.startswith(prefix):
        return None
    pid = key[len(prefix):]
    return pid if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", pid) else None


def qbittorrent_view(settings):
    """The `qbittorrent` block as an answer: the known behaviour keys only, whatever else is stored.
    The engine's connection details (and the password) are not settings and never pass through here."""
    block = (settings or {}).get("qbittorrent")
    block = block if isinstance(block, dict) else {}
    return {k: block.get(k, DEFAULTS["qbittorrent"][k]) for k in DEFAULTS["qbittorrent"]}


_CHECKS = {
    "library_root": _check_root,
    "ext_folders": _check_ext_folders,
    "rate_limit_kbps": lambda v: _int_in(v, 0, 10_000_000, "rate_limit_kbps"),
    "allow_private_hosts": _check_hosts,
    "concurrency.total": lambda v: _int_in(v, 1, 16, "concurrency.total"),
    "concurrency.http": lambda v: _int_in(v, 1, 16, "concurrency.http"),
    "concurrency.ytdlp": lambda v: _int_in(v, 1, 16, "concurrency.ytdlp"),
    "concurrency.stremio": lambda v: _int_in(v, 1, 16, "concurrency.stremio"),
    "concurrency.qbittorrent": lambda v: _int_in(v, 1, 16, "concurrency.qbittorrent"),
    "qbittorrent.prefer_for_magnets": _bool("qbittorrent.prefer_for_magnets"),
    "qbittorrent.cancel_deletes_data": _bool("qbittorrent.cancel_deletes_data"),
    "qbittorrent.after_done": _check_qb_after_done,
    "qbittorrent.save_subfolder": _check_kind_folder("qbittorrent.save_subfolder"),
    "qbittorrent.category": _check_qb_label("qbittorrent.category", 100),
    "qbittorrent.tag": _check_qb_label("qbittorrent.tag", 60),
    "qbittorrent.metadata_timeout_s": lambda v: _int_in(v, 60, 86400, "qbittorrent.metadata_timeout_s"),
    "qbittorrent.stall_minutes": lambda v: _int_in(v, 0, 10080, "qbittorrent.stall_minutes"),
    "qbittorrent.poll_s": lambda v: _float_in(v, 0.5, 30, "qbittorrent.poll_s"),
    "http.stall_seconds": lambda v: _int_in(v, 10, 3600, "http.stall_seconds"),
    "http.max_retries": lambda v: _int_in(v, 0, 10, "http.max_retries"),
    "stremio.stall_seconds": lambda v: _int_in(v, 10, 3600, "stremio.stall_seconds"),
    "stremio.max_retries": lambda v: _int_in(v, 0, 10, "stremio.max_retries"),
    "stremio.metadata_timeout": lambda v: _int_in(v, 10, 600, "stremio.metadata_timeout"),
    "stremio.start_wait_seconds": lambda v: _int_in(v, 10, 300, "stremio.start_wait_seconds"),
    "http.segments": lambda v: _int_in(v, 1, 16, "http.segments"),
    "http.segment_min_bytes": lambda v: _int_in(v, 1024 * 1024, 1 << 40, "http.segment_min_bytes"),
    "schedule": _check_schedule,
    "after_download.open": _check_open_mode,
    "notify.enabled": _bool("notify.enabled"),
    "notify.providers": _bool("notify.providers"),
    "notify.min_seconds": lambda v: _int_in(v, 0, 86400, "notify.min_seconds"),
    "notify.coalesce_s": lambda v: _int_in(v, 0, 600, "notify.coalesce_s"),
    "unknown_settle_hours": lambda v: _int_in(v, 1, 720, "unknown_settle_hours"),
    "retention.keep_history_days": _check_keep_history_days,
}
for _kind in FOLDER_KINDS:
    _CHECKS["folders." + _kind] = _check_kind_folder("folders." + _kind)
for _provider in NOTIFY_PROVIDERS:
    _CHECKS["notify.by_provider.%s" % _provider] = _bool("notify.by_provider.%s" % _provider)
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
        pid = notify_provider_key(key)
        if pid is not None and key not in allowed and "notify.providers" in allowed:
            # any provider id has its own notification switch, not only the two built in
            checked, err = _bool(key)(value)
            if err:
                errors.append(err)
            else:
                clean[key] = checked
            continue
        if key not in allowed:
            if key in WRITABLE:
                errors.append("%r can't be changed with dl_prefs; it needs dl_settings (which asks you first)" % str(key)[:60])
            else:
                errors.append("%r can't be changed here" % str(key)[:60])
            continue
        if key == "ext_folders":
            # A full path is only accepted from the confirm-gated tier (the one that may also
            # change library_root).
            checked, err = _check_ext_folders(value, allow_abs="library_root" in allowed)
        else:
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


# --------------------------------------------------------------------------- where files go

def ext_folder_spec(settings, name):
    """The owner's folder for this file name's extension (longest extension first), or None."""
    table = (settings or {}).get("ext_folders") or {}
    if not table:
        return None
    suffixes = [x.lower() for x in Path(str(name or "")).suffixes][-3:]
    for start in range(len(suffixes)):
        key = "".join(suffixes[start:])
        if table.get(key):
            return table[key]
    return None


def write_roots(settings, options=None):
    """Every folder the hub may write finished files into: the library root, plus any full-path
    folder the owner chose (an extension folder, or this download's own folder).  Everything that
    checks 'is this path ours?' asks this one question."""
    roots = [library_root(settings)]
    for spec in ((settings or {}).get("ext_folders") or {}).values():
        if is_absolute_spec(spec):
            full = abs_folder(spec)
            if full:
                roots.append(Path(full))
    extra = (options or {}).get("dest_abs")
    if extra and abs_folder(extra):
        roots.append(Path(abs_folder(extra)))
    return roots


def resolve_dest_dir(settings, name, kind, options=None):
    """(folder as a Path or None, source) for a download that is about to start writing.

    Order, first one that applies wins:
      1. this download's own folder   options `dest_abs` (a full path) or `subdir` (inside the library)
      2. the extension's folder       settings `ext_folders`
      3. the kind's folder            settings `folders` (Video / Audio / Files)
    A bad extension entry never stops a download: it is skipped and the next rule applies.  A bad
    folder on the download itself is an error (None), because the owner asked for that place.
    """
    options = options or {}
    root = library_root(settings)
    if options.get("dest_abs"):
        full = abs_folder(options["dest_abs"])
        return (Path(full), "download") if full else (None, "download")
    if options.get("subdir"):
        return naming.safe_subdir(root, options["subdir"]), "download"
    spec = ext_folder_spec(settings, name)
    if spec:
        if is_absolute_spec(spec):
            full = abs_folder(spec)
            if full:
                return Path(full), "extension"
        else:
            target = naming.safe_subdir(root, spec)
            if target is not None:
                return target, "extension"
    return Path(root) / naming.kind_folder(kind, settings.get("folders")), "kind"
