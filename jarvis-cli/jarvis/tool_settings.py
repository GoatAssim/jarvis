"""TOOL_SETTINGS -- settings a tool file supplies for the Settings menu.

WHY THIS EXISTS
---------------
A tool used to have no place to put a setting the owner may want to change (a
folder, a limit, a switch). The only options were a hand-edited JSON file or a
constant in the code. A tool file can now declare its own settings, and they show
up in the ordinary Settings window (Settings > Tool settings), next to everything
else -- not in a menu of the tool's own.

    TOOL_SETTINGS = [
        {
            "key": "max_notes",              # required: ^[a-z][a-z0-9_]{0,39}$, unique in the file
            "label": "Most notes kept",      # required: shown beside the control
            "type": "integer",               # "boolean" | "string" | "number" | "integer" | "choice"
            "default": 200,                  # optional: the type's own default when left out
            "help": "Older notes are dropped.",       # optional
            "min": 1, "max": 5000,           # optional, number / integer only
            "choices": ["a", "b"],           # choice only: values, or [value, "Label"] pairs
            "unit": "notes",                 # optional, number / integer only
            "section": "Limits",             # optional: a heading to group rows under
        },
    ]

and the tool reads its current value when it runs:

    from jarvis import tool_settings
    limit = tool_settings.get(__file__, "max_notes")        # the declared default if unset

WHAT IT IS (AND IS NOT)
-----------------------
* Values live in `~/.jarvis/tool_settings.json` as `{"<file stem>": {"<key>": value}}`.
  Only a value the owner CHANGED is stored; "reset" deletes the key.
* Only the owner can change one: the Settings window and `jarvis tool-settings`.
  There is deliberately NO model tool that writes here -- the model is the component
  most exposed to text other people wrote, and a setting can point a tool somewhere.
* Every value is coerced to the declared type and range on the way in AND on the way
  out (`get`), so a hand-edited file can never hand a tool a string where it asked
  for a number. A value that does not fit reads as the default.
* It holds no secrets. Don't put a token or password in a setting: the file is plain
  JSON and the Settings window shows what is in it.

Import-light on purpose: tool_loader.py imports this while tools.py is only half
initialised, so nothing here may import ai_client, tool_router, tool_registry or
tools at module level.
"""

import json
import os
import re
import threading
from pathlib import Path

TYPES = ("boolean", "string", "number", "integer", "choice")

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}

MAX_ENTRIES_PER_FILE = 40
MAX_LABEL = 80
MAX_HELP = 300
MAX_UNIT = 20
MAX_SECTION = 40
MAX_CHOICES = 40
MAX_CHOICE_TEXT = 60
MAX_STRING = 500            # the longest string value a setting may hold

ENCODING = "utf-8"
_LOCK = threading.Lock()
_MISSING = object()


def settings_path():
    """Resolved on every call: tests move HOME, and a long-lived worker should not
    keep a stale path."""
    return Path.home() / ".jarvis" / "tool_settings.json"


def stem_of(file):
    """'notes.py', '/x/tools/notes.py' and 'notes' all name the file 'notes'."""
    name = Path(str(file or "")).name
    return name[:-3] if name.lower().endswith(".py") else name


def _text(value, limit):
    return " ".join(str(value if value is not None else "").split())[:limit]


# ---------------------------------------------------------------------------
# values: coercion (the one place that decides what a value may be)
# ---------------------------------------------------------------------------

def coerce(entry, value):
    """(ok, value, error): `value` made into what `entry` declares, or why not."""
    kind = entry.get("type")
    if kind == "boolean":
        if isinstance(value, bool):
            return True, value, ""
        if isinstance(value, str) and value.strip().lower() in _TRUE:
            return True, True, ""
        if isinstance(value, str) and value.strip().lower() in _FALSE:
            return True, False, ""
        return False, None, "%s must be on or off" % entry["label"]
    if kind in ("integer", "number"):
        if isinstance(value, bool):
            return False, None, "%s must be a number" % entry["label"]
        try:
            number = float(value) if not isinstance(value, (int, float)) else value
        except (TypeError, ValueError):
            return False, None, "%s must be a number" % entry["label"]
        if number != number or number in (float("inf"), float("-inf")):
            return False, None, "%s must be a finite number" % entry["label"]
        if kind == "integer":
            if int(number) != number:
                return False, None, "%s must be a whole number" % entry["label"]
            number = int(number)
        low, high = entry.get("min"), entry.get("max")
        if low is not None and number < low:
            return False, None, "%s can't be below %s" % (entry["label"], low)
        if high is not None and number > high:
            return False, None, "%s can't be above %s" % (entry["label"], high)
        return True, number, ""
    if kind == "choice":
        allowed = [c[0] for c in entry.get("choices") or []]
        text = str(value) if not isinstance(value, bool) else ""
        if text in allowed:
            return True, text, ""
        return False, None, "%s must be one of: %s" % (entry["label"], ", ".join(allowed))
    # string
    if not isinstance(value, str):
        return False, None, "%s must be text" % entry["label"]
    if len(value) > MAX_STRING:
        return False, None, "%s is too long (at most %d characters)" % (entry["label"], MAX_STRING)
    if "\x00" in value:
        return False, None, "%s can't contain a null character" % entry["label"]
    return True, value, ""


def _type_default(kind, choices):
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return 0
    if kind == "choice":
        return choices[0][0] if choices else ""
    return ""


# ---------------------------------------------------------------------------
# validation (called by tool_loader)
# ---------------------------------------------------------------------------

def _validate_choices(raw, where, errors):
    if not isinstance(raw, (list, tuple)) or not raw:
        errors.append("%s: a choice setting needs a non-empty `choices` list" % where)
        return None
    if len(raw) > MAX_CHOICES:
        errors.append("%s: %d choices; at most %d are allowed" % (where, len(raw), MAX_CHOICES))
        return None
    out, seen = [], set()
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            value, label = item
        else:
            value, label = item, item
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            errors.append("%s: each choice must be text, or a [value, label] pair" % where)
            return None
        value = _text(value, MAX_CHOICE_TEXT)
        if not value or value in seen:
            errors.append("%s: choices must be non-empty and different from each other" % where)
            return None
        seen.add(value)
        out.append([value, _text(label, MAX_CHOICE_TEXT) or value])
    return out


def validate_settings(raw, filename):
    """Validate a module's TOOL_SETTINGS. Returns (entries, errors). A bad entry is
    dropped and reported; the rest still load (and so does the tool)."""
    errors = []
    if not isinstance(raw, (list, tuple)):
        return [], ["%s: TOOL_SETTINGS must be a list of dicts" % filename]
    items = list(raw)
    if len(items) > MAX_ENTRIES_PER_FILE:
        errors.append("%s: TOOL_SETTINGS has %d entries; only the first %d are used"
                      % (filename, len(items), MAX_ENTRIES_PER_FILE))
        items = items[:MAX_ENTRIES_PER_FILE]
    out, seen = [], set()
    for index, item in enumerate(items):
        where = "%s: TOOL_SETTINGS[%d]" % (filename, index)
        if not isinstance(item, dict):
            errors.append("%s is not a dict" % where)
            continue
        key = item.get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            errors.append("%s: key %r must be lower_snake_case (letters, digits, underscore; "
                          "starting with a letter; at most 40 characters)" % (where, key))
            continue
        if key in seen:
            errors.append("%s: duplicate key %r" % (where, key))
            continue
        label = _text(item.get("label"), MAX_LABEL)
        if not label:
            errors.append("%s (%s): label is required" % (where, key))
            continue
        kind = item.get("type", "string")
        if kind not in TYPES:
            errors.append("%s (%s): type %r must be one of %s" % (where, key, kind, ", ".join(TYPES)))
            continue
        choices = None
        if kind == "choice":
            choices = _validate_choices(item.get("choices"), "%s (%s)" % (where, key), errors)
            if choices is None:
                continue
        entry = {"key": key, "label": label, "type": kind,
                 "help": _text(item.get("help"), MAX_HELP),
                 "section": _text(item.get("section"), MAX_SECTION)}
        if choices is not None:
            entry["choices"] = choices
        if kind in ("integer", "number"):
            entry["unit"] = _text(item.get("unit"), MAX_UNIT)
            bad_range = False
            for bound in ("min", "max"):
                if item.get(bound) is None:
                    entry[bound] = None
                    continue
                ok, value, why = coerce({"type": kind, "label": bound}, item.get(bound))
                if not ok:
                    errors.append("%s (%s): %s is not usable (%s)" % (where, key, bound, why))
                    bad_range = True
                    break
                entry[bound] = value
            if bad_range:
                continue
            if entry["min"] is not None and entry["max"] is not None and entry["min"] > entry["max"]:
                errors.append("%s (%s): min is above max" % (where, key))
                continue
        # The default has to be a legal value of its own setting.
        if "default" in item and item.get("default") is not None:
            ok, value, why = coerce(entry, item.get("default"))
            if not ok:
                errors.append("%s (%s): default is not allowed (%s)" % (where, key, why))
                continue
            entry["default"] = value
        else:
            entry["default"] = _type_default(kind, choices)
            if kind in ("integer", "number"):
                # 0 may be outside the declared range: sit on the nearest legal value.
                ok, value, _why = coerce(entry, entry["default"])
                if not ok:
                    entry["default"] = entry["min"] if entry["min"] is not None else entry["max"]
        seen.add(key)
        out.append(entry)
    return out, errors


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _load():
    try:
        data = json.loads(settings_path().read_text(encoding=ENCODING))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def _save(data):
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding=ENCODING)
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        return False


def stored(file):
    """{key: value} the owner has changed for one file, as written (not coerced)."""
    block = _load().get(stem_of(file))
    return dict(block) if isinstance(block, dict) else {}


def value_of(entry, file, store=None):
    """(value, is_default) for one declared setting. A stored value that no longer fits
    the declaration (a hand edit, a changed range) reads as the default."""
    block = stored(file) if store is None else store
    if entry["key"] in block:
        ok, value, _why = coerce(entry, block[entry["key"]])
        if ok:
            return value, False
    return entry["default"], True


def set_value(entry, file, value):
    """Save one setting. Returns (ok, value, error); never raises."""
    ok, clean, why = coerce(entry, value)
    if not ok:
        return False, None, why
    with _LOCK:
        data = _load()
        block = data.get(stem_of(file))
        block = dict(block) if isinstance(block, dict) else {}
        block[entry["key"]] = clean
        data[stem_of(file)] = block
        if not _save(data):
            return False, None, "couldn't write %s" % settings_path()
    return True, clean, ""


def reset_value(entry, file):
    """Forget the owner's value for one setting (back to the declared default)."""
    with _LOCK:
        data = _load()
        block = data.get(stem_of(file))
        if isinstance(block, dict) and entry["key"] in block:
            block = dict(block)
            del block[entry["key"]]
            if block:
                data[stem_of(file)] = block
            else:
                data.pop(stem_of(file), None)
            if not _save(data):
                return False, "couldn't write %s" % settings_path()
    return True, ""


# ---------------------------------------------------------------------------
# what a tool calls
# ---------------------------------------------------------------------------

def get(file, key, default=_MISSING):
    """The current value of one of the calling file's settings.

        tool_settings.get(__file__, "max_notes")

    With no `default`, an unset (or unreadable) value is the declared default; pass a
    `default` to use your own when the declaration can't be found. Never raises."""
    try:
        block = stored(file)
        declared = _declared(file, key)
        if declared is not None:
            if key in block:
                ok, value, _why = coerce(declared, block[key])
                if ok:
                    return value
            return declared["default"] if default is _MISSING else default
        if key in block:
            return block[key]
    except Exception:  # noqa: BLE001 - a setting must never take a tool down
        pass
    return None if default is _MISSING else default


def _declared(file, key):
    """The declared entry for (file, key), or None. Looked up lazily so this module
    stays import-light."""
    try:
        from . import tools as system_tools
        stem = stem_of(file)
        for entry in getattr(system_tools, "AUTO_SETTINGS", []):
            if entry.get("file_stem") == stem and entry["key"] == key:
                return entry
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# what the Settings window and the CLI show
# ---------------------------------------------------------------------------

def public(entry, file, store=None):
    """One setting as the browser gets it (nothing private; the value is coerced)."""
    value, is_default = value_of(entry, file, store)
    item = {"key": entry["key"], "label": entry["label"], "type": entry["type"],
            "help": entry["help"], "section": entry["section"],
            "default": entry["default"], "value": value,
            "is_default": bool(is_default)}
    if entry["type"] == "choice":
        item["choices"] = [list(c) for c in entry["choices"]]
    if entry["type"] in ("integer", "number"):
        item["min"], item["max"], item["unit"] = entry["min"], entry["max"], entry["unit"]
    return item
