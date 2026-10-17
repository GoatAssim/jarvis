"""Hub settings: defaults and the one validator (plan section 13).

Stored in hub.db `settings` (one JSON value per top-level key), read as
defaults + overrides.  `validate_update` is the ONLY place a write is checked, so
`dl_settings`, the panel and a future CLI cannot drift apart.

Only a subset is writable today.  The rest of the plan's settings (schedule, retention,
Stremio, per-provider blocks) already have defaults so later phases read them without a
migration, but nothing can change them until the phase that owns them adds the key to
`WRITABLE`.
"""

import copy
from pathlib import Path

from . import paths

DEFAULTS = {
    "library_root": "",          # "" = paths.default_library_root()
    "concurrency": {"total": 4, "http": 3, "ytdlp": 2, "stremio": 3},
    "rate_limit_kbps": 0,        # 0 = unlimited; shared across running hub downloads
    "schedule": [],              # phase 8
    "retention": {"keep_history_days": 90, "auto_delete_files": False},
    "allow_private_hosts": [],   # the SSRF allow-list ("host" or "host:port")
    "http": {"stall_seconds": 60, "max_retries": 3, "connect_timeout": 15},
    "stremio": {"mode": "save", "stall_seconds": 90, "max_retries": 3},   # phase 4
    "providers": {},             # phase 5: per-provider blocks
    "provider_limits": {"max_rows": 500, "max_report_kb": 64, "max_reports_per_s": 20,
                        "stale_after_s": 30},
    "allowed_link_schemes": ["steam", "https"],
    "limits": {"max_items_per_call": 50, "confirm_above_items": 5, "max_bytes_per_call_gb": 150},
    "demo": {"enabled": False, "started_at": 0},
}

# key -> checker(value) -> (ok_value, error).  Nested keys are written as "a.b".
WRITABLE = {
    "library_root", "rate_limit_kbps", "allow_private_hosts",
    "concurrency.total", "concurrency.http", "concurrency.ytdlp", "concurrency.stremio",
    "http.stall_seconds", "http.max_retries",
}


def merged(stored):
    """Defaults with the stored overrides laid over them (one level deep for dicts)."""
    out = copy.deepcopy(DEFAULTS)
    for key, value in (stored or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key].update(value)
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
}


def validate_update(updates):
    """{"concurrency.http": 2, "library_root": "D:/x"} -> (clean, errors).

    Unknown or not-yet-writable keys are errors, never silently ignored: a typo in a
    setting name must not look like success.
    """
    clean, errors = {}, []
    if not isinstance(updates, dict) or not updates:
        return clean, ["nothing to change"]
    for key, value in updates.items():
        if key not in WRITABLE:
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
            block = dict(stored.get(head) or {})
            block[tail] = value
            stored[head] = block
        else:
            stored[head] = value
        changed.append(head)
    return sorted(set(changed))
