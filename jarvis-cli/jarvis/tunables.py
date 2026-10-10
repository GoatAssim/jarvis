"""Owner-adjustable knobs that used to need a source edit (L.43, Advanced layer 2).

Jarvis has about 258 module-level numeric constants and a handful of ``JARVIS_*``
environment switches. Nearly all of them are plumbing or deliberately fixed (some are
invariants in AGENTS.md). A few are genuinely "how much / how long" knobs an owner
could reasonably want to change from the Settings > Advanced screen without editing
code. This module is the one place that says WHICH ones, what type and range each
has, what its default is and when a change takes effect.

How a value is resolved (``get``), most specific first:

  1. the process environment, for an ``env`` tunable only (a shell or the scheduler
     that sets ``JARVIS_X`` for one run knows better than a global file);
  2. the override stored in ``~/.jarvis/tunables.json``;
  3. the default written here.

Rules that keep this safe:

* **A tunable that is not registered here cannot be set.** The registry is code, not
  a file, on purpose: adding one is a reviewed change. ``MAX_TOOL_ROUNDS``, the
  confirmation prompts, ``tool_safety`` and the risk review are NOT registered and
  must not be (AGENTS.md invariants).
* **Every stored value is validated against its registered type and range on write
  AND on read.** A hand-edited or out-of-range value is ignored (the default is used)
  rather than trusted, so a typo can never switch a protection off or crash a
  module's import.
* **``get`` never raises** and costs one small file read per process (cached by
  mtime). Jarvis is a fresh process per CLI call, so an override takes effect on the
  next ask; a long-lived process (the web server, a daemon) reads it once at start,
  which is what the ``applies`` field tells the owner.
* **Nothing model-facing writes here.** ``set_override`` / ``reset_override`` are
  called by ``settings_admin`` (the Settings screen's back end) and nothing else; no
  tool in ``tools.py`` imports them (``tests/test_settings_admin.py`` pins that).

Stdlib only, no import of any other jarvis module: it is imported by modules that
load very early (``memory``, ``key_health``), so it must never create a cycle.
"""

import json
import os
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
STORE_NAME = "tunables.json"
ENCODING = "utf-8"
STORE_VERSION = 1

# When a change takes effect. Shown next to every value in the screen.
APPLIES_NEXT_ASK = "next_ask"
APPLIES_RESTART_WEB = "restart_web"
APPLIES_LABELS = {
    APPLIES_NEXT_ASK: "Next ask (each ask is a fresh process)",
    APPLIES_RESTART_WEB: "After restarting the web server",
}

_OFF_VALUES = ("0", "off", "false", "no")
_ON_VALUES = ("1", "on", "true", "yes")

# --- the registry ----------------------------------------------------------------------
# kind    "env"   a JARVIS_* switch (the process environment wins over the stored value)
#         "const" a module-level constant, read through ``get`` at import
# type    "bool" | "int" | "float"
# section which Settings section lists it ("memory", "ai", "tools", "notifications", "web")
# file    the module that reads it (shown so the owner knows what it touches)
REGISTRY = (
    {"name": "JARVIS_RAW_ARCHIVE", "kind": "env", "type": "bool", "default": True,
     "section": "memory", "file": "raw_archive.py", "applies": APPLIES_NEXT_ASK,
     "label": "Write the raw event log",
     "help": "Every message, reply and tool run is appended to ~/.jarvis/events. Turning "
             "this off stops NEW lines being written (replay-from-log and exports of "
             "later turns lose their raw copy). The log is unbounded by design."},
    {"name": "JARVIS_WEB_STREAM", "kind": "env", "type": "bool", "default": True,
     "section": "web", "file": "web/server.js", "applies": APPLIES_NEXT_ASK,
     "label": "Stream replies into the web chat as they are written",
     "help": "Off = the reply appears once it is complete. Useful behind a proxy that "
             "buffers, or if a provider's streaming misbehaves."},
    {"name": "JARVIS_TICK_MS", "kind": "env", "type": "int", "default": 30000,
     "min": 5000, "max": 600000, "unit": "ms",
     "section": "web", "file": "web/server.js", "applies": APPLIES_RESTART_WEB,
     "label": "Web server tick interval",
     "help": "How often the web server checks scheduled jobs and daemons. Read once when "
             "the server starts."},

    {"name": "DEFAULT_MIN_ROUND_INTERVAL", "kind": "const", "type": "float", "default": 3.0,
     "min": 0.0, "max": 60.0, "unit": "s",
     "section": "ai", "file": "key_health.py", "applies": APPLIES_NEXT_ASK,
     "label": "Minimum gap between two requests on the same API key",
     "help": "A pacing floor so a tool-using ask does not hammer one key. 0 removes the "
             "wait (a provider block's own min_round_interval_seconds still wins)."},
    {"name": "AGENT_TIMEOUT", "kind": "const", "type": "int", "default": 150,
     "min": 30, "max": 900, "unit": "s",
     "section": "tools", "file": "custom_tools_agent.py", "applies": APPLIES_NEXT_ASK,
     "label": "Tool Manager 'Ask Jarvis': time allowed per attempt",
     "help": "How long one attempt at writing a tool may take before it is abandoned."},
    {"name": "MAX_CONTINUATIONS", "kind": "const", "type": "int", "default": 3,
     "min": 0, "max": 8,
     "section": "tools", "file": "custom_tools_agent.py", "applies": APPLIES_NEXT_ASK,
     "label": "Tool Manager 'Ask Jarvis': follow-up calls for a long file",
     "help": "How many times a reply cut off by the provider's output limit is continued."},
    {"name": "MAX_FACTS", "kind": "const", "type": "int", "default": 80,
     "min": 10, "max": 1000,
     "section": "memory", "file": "memory.py", "applies": APPLIES_NEXT_ASK,
     "label": "Most long-term facts kept",
     "help": "When the memory store is full the oldest facts are dropped to make room."},
    {"name": "MAX_PROMPT_FACTS", "kind": "const", "type": "int", "default": 8,
     "min": 0, "max": 50,
     "section": "memory", "file": "memory.py", "applies": APPLIES_NEXT_ASK,
     "label": "Most facts added to one prompt",
     "help": "Only facts matching the current question are added, up to this many. "
             "More facts means more tokens on every ask."},
    {"name": "MAX_INBOX", "kind": "const", "type": "int", "default": 500,
     "min": 50, "max": 5000,
     "section": "notifications", "file": "notifier.py", "applies": APPLIES_NEXT_ASK,
     "label": "Notifications kept in the inbox",
     "help": "Older notifications are dropped once the inbox holds this many."},
    {"name": "TOAST_TIMEOUT", "kind": "const", "type": "int", "default": 15,
     "min": 2, "max": 120, "unit": "s",
     "section": "notifications", "file": "notifier.py", "applies": APPLIES_NEXT_ASK,
     "label": "How long a desktop toast stays up",
     "help": "Applies to the Windows toast a notification raises."},
    {"name": "FETCH_TIMEOUT", "kind": "const", "type": "int", "default": 15,
     "min": 3, "max": 120, "unit": "s",
     "section": "tools", "file": "web_tools.py", "applies": APPLIES_NEXT_ASK,
     "label": "Web fetch / search timeout",
     "help": "How long the web tools wait for a site before giving up."},
    {"name": "FETCH_MAX_CHARS", "kind": "const", "type": "int", "default": 4000,
     "min": 500, "max": 50000, "unit": "chars",
     "section": "tools", "file": "web_tools.py", "applies": APPLIES_NEXT_ASK,
     "label": "Web fetch: most text returned to the model",
     "help": "A bigger number reads more of a page and costs more tokens."},
)

_BY_NAME = {t["name"]: t for t in REGISTRY}

# Environment variables Jarvis sets for ITSELF while it runs (one ask, one scheduled job,
# one chat message). Shown read-only in Advanced so the owner can see them exist; never
# editable, because a stored value here would be applied to the wrong process.
PLUMBING_ENV = (
    ("JARVIS_CONVERSATION_ID", "Which conversation this ask belongs to."),
    ("JARVIS_SCHEDULED", "Set while a scheduled job runs; makes the run unattended."),
    ("JARVIS_CONTEXT", "Names an unattended context for the safety policy."),
    ("JARVIS_CHANNEL", "Set while a chat-platform message is being answered."),
    ("JARVIS_CHANNEL_SENDER", "Who sent the chat message being answered."),
    ("JARVIS_ALLOWED_TOOLS", "The tool allow-list for the person being answered."),
    ("JARVIS_UI", "Set to 'web' by the web server so the CLI knows who is watching."),
    ("JARVIS_STREAM_MARKERS", "Set to 1 by the web server; the CLI then prints stream markers."),
    ("JARVIS_PROVIDER_OVERRIDE", "The provider order picked for this one ask."),
    ("JARVIS_THINK_OVERRIDE", "The thinking level picked for this one ask."),
    ("JARVIS_TOKEN_BUDGET", "Token limit set for one scheduled run."),
    ("JARVIS_TASK_ID", "The task or subagent this process is running."),
    ("JARVIS_CHANGE_SOURCE", "Marks a permission change as made from the web panel."),
    ("JARVIS_JOB_APPROVED_KINDS", "Tool kinds a scheduled job was pre-approved for."),
)


def registry():
    """The registered tunables, as plain dicts (a copy: callers can't edit the table)."""
    return [dict(t) for t in REGISTRY]


def spec(name):
    t = _BY_NAME.get(name)
    return dict(t) if t else None


# --- coercion and validation -----------------------------------------------------------
def coerce(entry, value):
    """Turn `value` into the registered type or raise ValueError with a reason that is
    fit to show the owner. Used for writes (strict) and for reads (the caller catches)."""
    kind = entry["type"]
    label = entry["label"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _ON_VALUES + _OFF_VALUES:
            return value.strip().lower() in _ON_VALUES
        raise ValueError("%s: expected on or off." % label)
    if isinstance(value, bool):
        raise ValueError("%s: expected a number, not on/off." % label)
    try:
        if kind == "int":
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            number = int(value)
        else:
            number = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s: %r is not a %s." % (label, value, "whole number" if kind == "int" else "number"))
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("%s: not a finite number." % label)
    lo, hi = entry.get("min"), entry.get("max")
    if lo is not None and number < lo:
        raise ValueError("%s: %s is below the minimum of %s." % (label, number, lo))
    if hi is not None and number > hi:
        raise ValueError("%s: %s is above the maximum of %s." % (label, number, hi))
    return number


# --- the store -------------------------------------------------------------------------
_cache = {"path": None, "mtime": None, "values": {}}


def store_path():
    return Path(JARVIS_DIR) / STORE_NAME


def _read_store():
    """{name: value} from tunables.json, or {} when absent/unreadable. Cached by mtime so
    a module that asks for ten constants at import reads the file once."""
    path = store_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache.update(path=str(path), mtime=None, values={})
        return {}
    if _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return _cache["values"]
    values = {}
    try:
        data = json.loads(path.read_text(encoding=ENCODING))
        raw = data.get("values") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            values = raw
    except (OSError, ValueError, UnicodeDecodeError):
        values = {}
    _cache.update(path=str(path), mtime=mtime, values=values)
    return values


def stored(name):
    """The stored override for `name` coerced and range-checked, or None. A stored value
    that fails validation is treated as absent, never trusted."""
    entry = _BY_NAME.get(name)
    if entry is None:
        return None
    values = _read_store()
    if name not in values:
        return None
    try:
        return coerce(entry, values[name])
    except ValueError:
        return None


def env_value(name):
    """The environment's value for an env tunable, coerced, or None when unset/unusable."""
    entry = _BY_NAME.get(name)
    if entry is None or entry["kind"] != "env":
        return None
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return coerce(entry, str(raw).strip())
    except ValueError:
        return None


def resolve(name):
    """(value, source) where source is 'env', 'override' or 'default'."""
    entry = _BY_NAME.get(name)
    if entry is None:
        return None, "unregistered"
    if entry["kind"] == "env":
        value = env_value(name)
        if value is not None:
            return value, "env"
    value = stored(name)
    if value is not None:
        return value, "override"
    return entry["default"], "default"


def get(name, default=None):
    """The effective value of a registered tunable. For an unregistered name, `default`
    (so a stray call can never invent a setting). Never raises."""
    try:
        if name not in _BY_NAME:
            return default
        return resolve(name)[0]
    except Exception:  # noqa: BLE001 -- a settings read must never take a module down
        entry = _BY_NAME.get(name)
        return entry["default"] if entry else default


def const(name, default):
    """A module constant, overridable. `default` is the literal the module always had;
    it is what you get when the name is unregistered, unset, or the stored value is bad."""
    entry = _BY_NAME.get(name)
    if entry is None or entry["kind"] != "const":
        return default
    value = get(name, default)
    # Keep an int an int when the registry says float and the module's literal is an int
    # (and vice versa): the module's own arithmetic was written for its literal's type.
    try:
        if isinstance(default, int) and not isinstance(default, bool):
            return int(value)
        if isinstance(default, float):
            return float(value)
    except (TypeError, ValueError):
        return default
    return value


def snapshot():
    """Every tunable with its effective value and where it came from, for the screen."""
    out = []
    values = _read_store()
    for entry in REGISTRY:
        value, source = resolve(entry["name"])
        row = dict(entry)
        row["value"] = value
        row["source"] = source
        row["has_override"] = entry["name"] in values
        row["override"] = stored(entry["name"])
        # An env value that is set but beats a stored override: say so, the owner would
        # otherwise change the field and see nothing happen.
        row["env_wins"] = bool(entry["kind"] == "env" and source == "env" and row["has_override"])
        row["env_value"] = env_value(entry["name"]) if entry["kind"] == "env" else None
        row["applies_label"] = APPLIES_LABELS.get(entry["applies"], entry["applies"])
        out.append(row)
    return out


# --- writers (called by settings_admin only) -------------------------------------------
def _write_store(values):
    """Atomic write of the whole store. Returns True on success. Uses atomic_io when it can
    be imported (it only depends on the stdlib) so the .bak convention is shared."""
    from . import atomic_io

    payload = {"version": STORE_VERSION, "values": values}
    ok = atomic_io.write_json(store_path(), payload)
    _cache.update(path=None, mtime=None, values={})
    return ok


def set_override(name, value):
    """Validate and store one override. Returns (True, coerced_value) or (False, reason)."""
    entry = _BY_NAME.get(name)
    if entry is None:
        return False, "%r is not a registered tunable." % name
    try:
        coerced = coerce(entry, value)
    except ValueError as exc:
        return False, str(exc)
    values = dict(_read_store())
    values[name] = coerced
    # Drop anything the registry no longer knows, so the file can't accumulate dead keys.
    values = {k: v for k, v in values.items() if k in _BY_NAME}
    if not _write_store(values):
        return False, "Couldn't write %s." % STORE_NAME
    return True, coerced


def reset_override(name):
    """Remove one override (back to the default). Returns (True, None) or (False, reason)."""
    if name not in _BY_NAME:
        return False, "%r is not a registered tunable." % name
    values = dict(_read_store())
    if name not in values:
        return True, None
    values.pop(name)
    values = {k: v for k, v in values.items() if k in _BY_NAME}
    if not _write_store(values):
        return False, "Couldn't write %s." % STORE_NAME
    return True, None
