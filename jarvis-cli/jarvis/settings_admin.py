"""Back end of the Settings > Advanced screen (L.43).

Advanced lets the owner change any setting in the project from the web UI. Three
layers, one module that owns every rule so the CLI and the web server cannot disagree
(REPO_MAP.md section 6: a web route shells out, it never reimplements a rule):

  1. **Files**: the JSON files under ~/.jarvis, edited as a document.
  2. **Tunables**: a short, curated list of numbers and switches that used to need a
     source edit (``tunables.py`` is the registry and the store).
  3. **Plumbing**: the ``JARVIS_*`` variables Jarvis sets for itself, shown read-only.

Why this is not just "the Config editor again" (the audit's finding 5: that route has no
auth and no validation). Every write here goes through the same gate, in this order:

  * the process is not a chat reply, a scheduled job or a subagent (``refuse_unattended``):
    settings change from the owner's screen or terminal, never from a message someone
    sent or a job running while nobody is looking;
  * the file is one we are willing to write (``group``): state files and files another
    panel owns are shown but read-only, so this screen is not a second, unguarded path
    around a panel's own rules (L.36's "per-person permissions fail closed");
  * the document parses, keeps the type of every value it already had, and passes the
    file's own checks (``validate``) -- all problems are reported at once;
  * the file has not changed since the owner opened it (``base_version``), so two tabs or
    a hand edit are never silently overwritten;
  * a guarded file (safety, permissions) needs ``confirm_guarded``;
  * secrets are masked on read and put back on write, so a token never reaches the
    browser unless the owner explicitly asks to reveal it;
  * the previous contents are copied to ``settings_backups/`` first (newest
    ``KEEP_BACKUPS`` kept) and the write is atomic (``atomic_io``), so a bad edit is one
    click to undo and a power cut cannot leave half a file;
  * the change is logged (file, kind, paths only -- never values) to
    ``settings_backups/changes.jsonl``.

Nothing the model can call imports this module (``tests/test_settings_admin.py`` pins
that), and ``AGENTS.md`` lists it under the invariants.
"""

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import atomic_io, tunables

JARVIS_DIR = Path.home() / ".jarvis"
ENCODING = "utf-8"

BACKUP_DIR_NAME = "settings_backups"
CHANGE_LOG_NAME = "changes.jsonl"
KEEP_BACKUPS = 20
CHANGE_LOG_MAX_BYTES = 256 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIFF_ENTRIES = 200
MAX_VALUE_PREVIEW = 120

# What a masked secret looks like in the editor. Never a valid secret itself.
MASK = "\u2022" * 8

GROUP_SETTINGS = "settings"   # editable
GROUP_SAFETY = "safety"       # editable, needs confirm_guarded
GROUP_MANAGED = "managed"     # another screen owns it: read-only here
GROUP_STATE = "state"         # a cache or counter Jarvis rebuilds: read-only here
EDITABLE_GROUPS = (GROUP_SETTINGS, GROUP_SAFETY)

A_NEXT_ASK = "Next ask."
A_NEXT_RUN = "Next time it runs."
A_FEATURE = "When the feature next runs. A daemon that is already running may need a restart."
A_UNKNOWN = "Not recorded for this file."

# name -> (group, title, applies, note)
_FILES = {
    "ai_config.json": (GROUP_SETTINGS, "AI providers, keys, persona and defaults", A_NEXT_ASK,
                       "Read fresh on every ask. API keys are hidden unless you reveal them."),
    "commands.json": (GROUP_SETTINGS, "Saved commands", A_NEXT_RUN,
                      "The Commands panel edits the same file. Each command is checked the way the builder checks it."),
    "notify_config.json": (GROUP_SETTINGS, "Notification levels and channels", A_FEATURE, ""),
    "digest_config.json": (GROUP_SETTINGS, "Daily digest", A_FEATURE, ""),
    "ambient.json": (GROUP_SETTINGS, "Ambient suggestions", A_FEATURE, ""),
    "calendar.json": (GROUP_SETTINGS, "Calendar sources", A_FEATURE, ""),
    "everything.json": (GROUP_SETTINGS, "Everything search", A_FEATURE, ""),
    "playnite.json": (GROUP_SETTINGS, "Playnite", A_FEATURE, ""),
    "spotify.json": (GROUP_SETTINGS, "Spotify", A_FEATURE, "Tokens are hidden unless you reveal them."),
    "voice_config.json": (GROUP_SETTINGS, "Voice", A_FEATURE, ""),
    "mcp_config.json": (GROUP_SETTINGS, "MCP servers", A_FEATURE,
                        "Every value under env and headers is hidden unless you reveal it."),
    "disabled.json": (GROUP_SETTINGS, "Tool Manager switches", A_NEXT_ASK,
                      "The Tool Manager writes the same file."),
    "memory.json": (GROUP_SETTINGS, "Long-term memory", A_NEXT_ASK, ""),
    "onboarding.json": (GROUP_SETTINGS, "First-run state", A_FEATURE, ""),
    "tool_safety.json": (GROUP_SAFETY, "Tool safety", A_NEXT_ASK,
                         "Decides which tool calls ask before they run."),
    "policy.json": (GROUP_SAFETY, "Unattended-run policy", A_NEXT_RUN,
                    "Decides what a scheduled or chat-triggered run may do."),
    "channels.json": (GROUP_SAFETY, "Chat platforms and who may talk to Jarvis", A_FEATURE,
                      "The allow-lists that decide who is answered."),
    "tunables.json": (GROUP_MANAGED, "Saved tunables", A_NEXT_ASK,
                      "Edit these in the Tunables list; each value is range-checked there."),
    "scheduled.json": (GROUP_MANAGED, "Scheduled jobs", A_NEXT_RUN, "Managed by the Schedules panel."),
    "daemons.json": (GROUP_MANAGED, "Daemons", A_FEATURE, "Managed by the Daemons panel."),
    "backlog.json": (GROUP_MANAGED, "Backlog", A_NEXT_RUN, "Managed by the Backlog panel."),
}
for _state in ("key_health.json", "usage_stats.json", "notifications.json", "conversation_history.json",
               "current_conversation.json", "ambient_state.json", "calendar_cache.json", "digest_queue.json",
               "discovery_cache.json", "history_summary_cache.json", "mcp_cache.json", "memory_index.json",
               "memory_namespace.json", "consolidation.json", "route_stickiness.json", "skill_stickiness.json"):
    _FILES[_state] = (GROUP_STATE, "State Jarvis keeps for itself", A_UNKNOWN,
                      "A counter, cache or inbox Jarvis rewrites on its own. Shown so you can look; not editable here.")

GROUP_LABELS = {
    GROUP_SETTINGS: "Settings",
    GROUP_SAFETY: "Safety and access (asks you to confirm)",
    GROUP_MANAGED: "Managed by another screen (read-only here)",
    GROUP_STATE: "State (read-only here)",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,80}\.json$")
_SUBDIR = "channels"
_BACKUP_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}_[0-9]{6}$")

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?keys?|token|secret|passw|authoriz|bearer|credential|private[_-]?key)", re.IGNORECASE)
# Files where every string under these keys is hidden, whatever the value looks like.
_MASK_ALL_UNDER = {"mcp_config.json": {"env", "headers"}}


# --- who may write ---------------------------------------------------------------------
def refuse_unattended():
    """A reason string if this process must not change settings, else None.

    A chat message, a scheduled job and a subagent all run the same CLI. A setting that
    can be changed from there can be changed by whoever can steer that conversation, so
    writes only work from a process that has none of those markers (the web server's
    Settings routes and the owner's own terminal).
    """
    for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID"):
        if os.environ.get(var):
            return ("Settings can only be changed from the Settings screen or your own terminal, "
                    "not from a chat message, a scheduled job or a background task.")
    if os.environ.get("JARVIS_CONTEXT") in ("scheduled", "unattended"):
        return ("Settings can only be changed from the Settings screen or your own terminal, "
                "not from a scheduled or unattended run.")
    return None


# --- names and paths -------------------------------------------------------------------
def _dir():
    return Path(JARVIS_DIR)


def _split_name(name):
    """('channels', 'people.json') or ('', 'ai_config.json'); None when the name is not one
    we will touch. Only top-level files and the channels/ folder are reachable."""
    if not isinstance(name, str):
        return None
    if "/" in name:
        head, _, tail = name.partition("/")
        if head != _SUBDIR or not _NAME_RE.match(tail):
            return None
        return head, tail
    if not _NAME_RE.match(name):
        return None
    return "", name


def _path_for(name):
    parts = _split_name(name)
    if parts is None:
        return None
    sub, leaf = parts
    return (_dir() / sub / leaf) if sub else (_dir() / leaf)


def meta_for(name):
    """The dict describing a file: group, title, applies, note, editable, guarded."""
    parts = _split_name(name)
    sub = parts[0] if parts else ""
    if sub == _SUBDIR:
        group, title, applies, note = (
            GROUP_MANAGED, "Chat-platform data", A_FEATURE,
            "Written by the Channels panel under its own permission rules, so it is read-only here.")
    elif name in _FILES:
        group, title, applies, note = _FILES[name]
    else:
        group, title, applies, note = (GROUP_SETTINGS, "", A_UNKNOWN,
                                       "Not one of the files Jarvis knows by name; edited as a plain document.")
    return {
        "name": name, "group": group, "group_label": GROUP_LABELS[group], "title": title,
        "applies": applies, "note": note,
        "editable": group in EDITABLE_GROUPS, "guarded": group == GROUP_SAFETY,
    }


def _raw(path):
    """(text or None, error or None). A missing file is (None, None)."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, None
    if size > MAX_FILE_BYTES:
        return None, "This file is %.1f MB, too large to edit here." % (size / 1048576.0)
    try:
        return path.read_text(encoding=ENCODING), None
    except UnicodeDecodeError:
        return None, "This file is not valid UTF-8 text."
    except OSError as exc:
        return None, "Couldn't read the file: %s" % exc


def version_of(text):
    """A short fingerprint of the file's contents. 'absent' when the file does not exist."""
    if text is None:
        return "absent"
    return hashlib.sha256(text.encode(ENCODING)).hexdigest()[:16]


def _parse(text):
    """(doc, None) or (None, message with line and column)."""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, "Not valid JSON: %s (line %d, column %d)." % (exc.msg, exc.lineno, exc.colno)
    except (TypeError, ValueError) as exc:
        return None, "Not valid JSON: %s." % exc


def _dump(doc):
    return json.dumps(doc, indent=2, ensure_ascii=False)


# --- secrets ---------------------------------------------------------------------------
def _key_hides(key, name, inherited):
    k = str(key)
    return inherited or bool(_SECRET_KEY_RE.search(k)) or k.lower() in _MASK_ALL_UNDER.get(name, ())


def mask(node, name, secret=False):
    """A copy of `node` with every secret string replaced by MASK. Empty strings stay empty
    (so an unset key still looks unset)."""
    if isinstance(node, dict):
        return {k: mask(v, name, _key_hides(k, name, secret)) for k, v in node.items()}
    if isinstance(node, list):
        return [mask(v, name, secret) for v in node]
    if secret and isinstance(node, str) and node != "":
        return MASK
    return node


def count_masked(node):
    if isinstance(node, dict):
        return sum(count_masked(v) for v in node.values())
    if isinstance(node, list):
        return sum(count_masked(v) for v in node)
    return 1 if node == MASK else 0


def unmask(new, old, path=()):
    """Put the real value back wherever the editor still shows MASK. Returns
    (document, problems). A MASK with no value at the same position in the old document
    (the list was reordered, the key renamed) cannot be mapped back and is a problem: the
    safe answer is to ask the owner to reveal, not to guess."""
    problems = []

    def walk(n, o, p):
        if isinstance(n, dict):
            out = {}
            for k, v in n.items():
                child = o.get(k) if isinstance(o, dict) else None
                out[k] = walk(v, child, p + (k,))
            return out
        if isinstance(n, list):
            out = []
            for i, v in enumerate(n):
                child = o[i] if isinstance(o, list) and i < len(o) else None
                out.append(walk(v, child, p + (i,)))
            return out
        if n == MASK:
            if isinstance(o, str):
                return o
            problems.append("%s is a hidden value that moved or was renamed; reveal secrets to edit it."
                            % _fmt_path(p))
            return n
        return n

    return walk(new, old, path), problems


def _fmt_path(path):
    out = ""
    for part in path:
        out += "[%d]" % part if isinstance(part, int) else (("." if out else "") + str(part))
    return out or "(top level)"


def _path_hidden(path, name):
    return any(_key_hides(p, name, False) for p in path if isinstance(p, str))


# --- diff ------------------------------------------------------------------------------
def _short(value):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_VALUE_PREVIEW else text[:MAX_VALUE_PREVIEW - 1] + "\u2026"


def diff(old, new, name):
    """A short list of what changed: [{path, kind, before, after}]. Values at secret paths
    are shown as the mask; a changed secret still shows as changed."""
    out = []

    def add(path, kind, before, after):
        if len(out) >= MAX_DIFF_ENTRIES:
            return
        hidden = _path_hidden(path, name)
        out.append({
            "path": _fmt_path(path), "kind": kind,
            "before": None if kind == "added" else (MASK if hidden and before not in ("", None) else _short(before)),
            "after": None if kind == "removed" else (MASK if hidden and after not in ("", None) else _short(after)),
        })

    def walk(o, n, p):
        if isinstance(o, dict) and isinstance(n, dict):
            for k in o:
                if k not in n:
                    add(p + (k,), "removed", o[k], None)
            for k in n:
                if k not in o:
                    add(p + (k,), "added", None, n[k])
                else:
                    walk(o[k], n[k], p + (k,))
            return
        if isinstance(o, list) and isinstance(n, list):
            for i in range(max(len(o), len(n))):
                if i >= len(n):
                    add(p + (i,), "removed", o[i], None)
                elif i >= len(o):
                    add(p + (i,), "added", None, n[i])
                else:
                    walk(o[i], n[i], p + (i,))
            return
        if o != n or type(o) is not type(n):
            add(p, "changed", o, n)

    walk(old, new, ())
    return out


# --- validation ------------------------------------------------------------------------
def _kind(value):
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "null"


def _stability(old, new, path, problems):
    """A value that exists in both documents must keep its kind (a number stays a number).
    null may become anything and anything may become null: both are how a setting is
    cleared or first filled."""
    if isinstance(old, dict) and isinstance(new, dict):
        for k in old:
            if k in new:
                _stability(old[k], new[k], path + (k,), problems)
        return
    if isinstance(old, list) and isinstance(new, list):
        for i in range(min(len(old), len(new))):
            if isinstance(old[i], (dict, list)) and isinstance(new[i], type(old[i])):
                _stability(old[i], new[i], path + (i,), problems)
        return
    if old is None or new is None:
        return
    if _kind(old) != _kind(new):
        problems.append("%s was %s and is now %s; keep its type." % (_fmt_path(path), _kind(old), _kind(new)))


def _validate_ai_config(doc, problems):
    try:
        from .ai_config import KNOWN_TYPES
    except Exception:  # noqa: BLE001
        KNOWN_TYPES = {"openai_compatible", "anthropic", "gemini", "cohere", "ollama"}
    providers = doc.get("providers")
    if providers is not None:
        if not isinstance(providers, list):
            problems.append("providers must be a list.")
        else:
            seen = set()
            for i, p in enumerate(providers):
                where = "providers[%d]" % i
                if not isinstance(p, dict):
                    problems.append("%s must be an object." % where)
                    continue
                name = p.get("name")
                if not isinstance(name, str) or not name.strip():
                    problems.append("%s needs a name." % where)
                elif name in seen:
                    problems.append("%s: the name %r is used twice." % (where, name))
                else:
                    seen.add(name)
                if p.get("type") not in KNOWN_TYPES:
                    problems.append("%s: type must be one of %s." % (where, ", ".join(sorted(KNOWN_TYPES))))
                if "enabled" in p and not isinstance(p["enabled"], bool):
                    problems.append("%s.enabled must be true or false." % where)
                for field in ("model", "base_url"):
                    if field in p and not isinstance(p[field], str):
                        problems.append("%s.%s must be text." % (where, field))
                url = p.get("base_url")
                if isinstance(url, str) and url and not re.match(r"^https?://", url):
                    problems.append("%s.base_url must start with http:// or https://." % where)
                keys = p.get("api_keys")
                if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
                    problems.append("%s.api_keys must be a list of text." % where)
    defaults = doc.get("defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            problems.append("defaults must be an object.")
        else:
            for field, lo in (("max_tokens", 1), ("scheduled_token_budget", 0),
                              ("compact_history_char_budget", 100), ("compact_history_exchanges", 0)):
                if field in defaults:
                    v = defaults[field]
                    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                        problems.append("defaults.%s must be a whole number, %d or more." % (field, lo))
            if "timeout" in defaults:
                v = defaults["timeout"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                    problems.append("defaults.timeout must be a number above 0.")
            for field in ("tools_enabled", "compact_prompt", "prompt_cache", "prompt_cache_tools",
                          "prompt_cache_key", "gemini_explicit_cache"):
                if field in defaults and not isinstance(defaults[field], bool):
                    problems.append("defaults.%s must be true or false." % field)
            pri = defaults.get("provider_priority")
            if pri is not None and (not isinstance(pri, list) or not all(isinstance(x, str) for x in pri)):
                problems.append("defaults.provider_priority must be a list of provider names.")
    persona = doc.get("persona")
    if persona is not None:
        if not isinstance(persona, dict):
            problems.append("persona must be an object.")
        else:
            for field, value in persona.items():
                if not isinstance(value, str):
                    problems.append("persona.%s must be text." % field)


def _validate_commands(doc, problems):
    commands = doc.get("commands")
    if commands is None:
        return
    if not isinstance(commands, dict):
        problems.append("commands must be an object of name -> command.")
        return
    try:
        from . import commands_config
    except Exception:  # noqa: BLE001
        return
    for cmd_name, spec in commands.items():
        msg = commands_config.validate_command_name(cmd_name)
        if msg:
            problems.append("commands.%s: %s" % (cmd_name, msg))
            continue
        msg = commands_config.validate_command_spec(spec)
        if msg:
            problems.append("commands.%s: %s" % (cmd_name, msg))


_VALIDATORS = {"ai_config.json": _validate_ai_config, "commands.json": _validate_commands}


def validate(name, new_doc, old_doc):
    """Every reason `new_doc` must not be written, as a list of strings (empty = fine)."""
    problems = []
    if old_doc is not None and type(old_doc) is not type(new_doc):
        problems.append("The top level must stay %s." % ("an object" if isinstance(old_doc, dict) else "a list"))
        return problems
    if old_doc is None and not isinstance(new_doc, (dict, list)):
        problems.append("The top level must be an object or a list.")
        return problems
    if old_doc is not None:
        _stability(old_doc, new_doc, (), problems)
    check = _VALIDATORS.get(name)
    if check and isinstance(new_doc, dict):
        check(new_doc, problems)
    return problems


# --- backups and the change log --------------------------------------------------------
def _backup_dir(name):
    return _dir() / BACKUP_DIR_NAME / name.replace("/", "__").replace(".json", "")


def _now_stamp(folder=None):
    """A sortable backup id. A clock with coarse ticks (Windows can be ~15 ms) could hand two
    saves the same stamp and the second would overwrite the first, so step forward until
    the name is free."""
    moment = datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%S_%f")
    while folder is not None and (folder / (stamp + ".json")).exists():
        moment += timedelta(microseconds=1)
        stamp = moment.strftime("%Y%m%dT%H%M%S_%f")
    return stamp


def _make_backup(name, text):
    """Copy `text` (the file as it is NOW) into the backup folder. Returns the id or None."""
    if text is None:
        return None
    folder = _backup_dir(name)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = _now_stamp(folder)
        (folder / (stamp + ".json")).write_text(text, encoding=ENCODING)
        for old in sorted(folder.glob("*.json"))[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
        return stamp
    except OSError:
        return None


def list_backups(name):
    if _split_name(name) is None:
        return {"ok": False, "error": "Unknown file."}
    folder = _backup_dir(name)
    rows = []
    if folder.is_dir():
        for p in sorted(folder.glob("*.json"), reverse=True):
            if not _BACKUP_ID_RE.match(p.stem):
                continue
            try:
                text = p.read_text(encoding=ENCODING)
                size = p.stat().st_size
            except (OSError, UnicodeDecodeError):
                continue
            stamp = datetime.strptime(p.stem, "%Y%m%dT%H%M%S_%f").replace(tzinfo=timezone.utc)
            rows.append({"id": p.stem, "at": stamp.isoformat(timespec="seconds"), "size": size,
                         "version": version_of(text)})
    return {"ok": True, "name": name, "backups": rows, "keep": KEEP_BACKUPS}


def _log_change(name, action, paths):
    try:
        folder = _dir() / BACKUP_DIR_NAME
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / CHANGE_LOG_NAME
        line = json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "file": name, "action": action, "paths": list(paths)[:50],
        }, ensure_ascii=False)
        if log.exists() and log.stat().st_size > CHANGE_LOG_MAX_BYTES:
            keep = log.read_text(encoding=ENCODING).splitlines()[-500:]
            log.write_text("\n".join(keep) + "\n", encoding=ENCODING)
        with log.open("a", encoding=ENCODING) as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# --- the catalog and reads -------------------------------------------------------------
def _known_names():
    names = set()
    d = _dir()
    try:
        for p in d.glob("*.json"):
            if _NAME_RE.match(p.name):
                names.add(p.name)
        for p in (d / _SUBDIR).glob("*.json"):
            if _NAME_RE.match(p.name):
                names.add(_SUBDIR + "/" + p.name)
    except OSError:
        pass
    # Files Jarvis knows by name are listed even before they exist, so a setting file that
    # has not been created yet can still be created from here.
    names.update(n for n, v in _FILES.items() if v[0] in EDITABLE_GROUPS)
    return sorted(names)


def catalog():
    files = []
    for name in _known_names():
        info = meta_for(name)
        path = _path_for(name)
        try:
            st = path.stat()
            info["exists"], info["size"] = True, st.st_size
            info["modified"] = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")
        except OSError:
            info["exists"], info["size"], info["modified"] = False, 0, None
        files.append(info)
    order = {g: i for i, g in enumerate((GROUP_SETTINGS, GROUP_SAFETY, GROUP_MANAGED, GROUP_STATE))}
    files.sort(key=lambda f: (order[f["group"]], f["name"]))
    try:
        from . import build_info
        version = build_info.version_string()
    except Exception:  # noqa: BLE001 -- About is informational; never fail the catalog for it
        version = ""
    return {
        "ok": True,
        "version": version,
        "jarvis_dir": str(_dir()),
        "files": files,
        "groups": [{"id": g, "label": GROUP_LABELS[g]} for g in order],
        "tunables": tunables.snapshot(),
        "plumbing": [{"name": n, "help": h} for n, h in tunables.PLUMBING_ENV],
        "tunables_file": str(tunables.store_path()),
        "backup_keep": KEEP_BACKUPS,
    }


def read(name, reveal=False):
    path = _path_for(name)
    if path is None:
        return {"ok": False, "error": "That is not a file this screen can open."}
    info = meta_for(name)
    text, err = _raw(path)
    if err:
        return {"ok": False, "error": err, "meta": info}
    out = {"ok": True, "name": name, "meta": info, "exists": text is not None,
           "version": version_of(text), "revealed": bool(reveal), "parse_ok": True,
           "path": str(path)}
    if text is None:
        out.update(text="{}\n", masked=0)
        return out
    doc, perr = _parse(text)
    if perr:
        # A file that doesn't parse is shown as it is so it can be fixed by hand.
        out.update(parse_ok=False, parse_error=perr, text=text, masked=0)
        return out
    shown = doc if reveal else mask(doc, name)
    out.update(text=_dump(shown) + "\n", masked=0 if reveal else count_masked(shown))
    return out


# --- writes ----------------------------------------------------------------------------
def _gate(name, confirm_guarded):
    """(meta, path, None) when a write may go ahead; (None, None, refusal dict) when not."""
    why = refuse_unattended()
    if why:
        return None, None, {"ok": False, "error": why, "code": "unattended"}
    path = _path_for(name)
    if path is None:
        return None, None, {"ok": False, "error": "That is not a file this screen can write.", "code": "bad_name"}
    info = meta_for(name)
    if not info["editable"]:
        return None, None, {"ok": False, "code": "read_only",
                            "error": "%s is read-only here. %s" % (name, info["note"])}
    if info["guarded"] and not confirm_guarded:
        return None, None, {"ok": False, "code": "needs_confirm", "guarded": True,
                            "error": "%s controls what Jarvis is allowed to do. Confirm that you "
                                     "mean to change it." % name}
    return info, path, None


def _prepare(name, new_doc, old_text, old_doc):
    """Unmask, validate and diff. Returns (doc, changes, problems)."""
    doc, problems = unmask(new_doc, old_doc) if old_doc is not None else (new_doc, [])
    if old_text is not None and old_doc is None:
        # The file exists but did not parse: nothing to compare against, any valid document
        # replaces it (that is how a broken file is repaired).
        problems += validate(name, doc, None)
    else:
        problems += validate(name, doc, old_doc)
    if old_doc is None and count_masked(doc):
        problems.append("A hidden-value placeholder can't be saved into a file that has no value there yet; "
                        "type the real value.")
    changes = diff(old_doc if old_doc is not None else ({} if isinstance(doc, dict) else []), doc, name)
    return doc, changes, problems


def preview(name, text, base_version=None):
    """What a save WOULD do. Writes nothing; refuses nothing but bad input."""
    path = _path_for(name)
    if path is None:
        return {"ok": False, "error": "That is not a file this screen can open."}
    info = meta_for(name)
    old_text, err = _raw(path)
    if err:
        return {"ok": False, "error": err}
    new_doc, perr = _parse(text)
    if perr:
        return {"ok": True, "valid": False, "problems": [perr], "changes": [], "meta": info}
    old_doc, _ = _parse(old_text) if old_text is not None else (None, None)
    doc, changes, problems = _prepare(name, new_doc, old_text, old_doc)
    stale = base_version is not None and base_version != version_of(old_text)
    if stale:
        problems = ["The file changed on disk after you opened it. Reload before saving."] + problems
    return {"ok": True, "valid": not problems, "problems": problems, "changes": changes,
            "stale": stale, "guarded": info["guarded"], "editable": info["editable"], "meta": info,
            "version": version_of(old_text), "secrets_changed": any(c["before"] == MASK or c["after"] == MASK for c in changes)}


def _commit(name, path, doc, old_text, action, changes):
    backup_id = _make_backup(name, old_text)
    if old_text is not None and backup_id is None:
        return {"ok": False, "error": "Couldn't make a backup first, so nothing was changed.", "code": "no_backup"}
    if not atomic_io.write_json(path, doc):
        return {"ok": False, "error": "Couldn't write %s. Nothing was changed." % name, "code": "write_failed"}
    _log_change(name, action, [c["path"] for c in changes])
    new_text, _ = _raw(path)
    return {"ok": True, "changed": True, "version": version_of(new_text), "backup": backup_id,
            "changes": len(changes)}


def write(name, text, base_version, confirm_guarded=False):
    info, path, refusal = _gate(name, confirm_guarded)
    if refusal:
        return refusal
    old_text, err = _raw(path)
    if err:
        return {"ok": False, "error": err}
    if base_version != version_of(old_text):
        return {"ok": False, "code": "stale", "version": version_of(old_text),
                "error": "The file changed on disk after you opened it. Reload it, then make your edit again."}
    new_doc, perr = _parse(text)
    if perr:
        return {"ok": False, "code": "invalid", "problems": [perr], "error": perr}
    old_doc, _ = _parse(old_text) if old_text is not None else (None, None)
    doc, changes, problems = _prepare(name, new_doc, old_text, old_doc)
    if problems:
        return {"ok": False, "code": "invalid", "problems": problems,
                "error": "Not saved: %d problem%s." % (len(problems), "" if len(problems) == 1 else "s")}
    if old_doc is not None and doc == old_doc:
        return {"ok": True, "changed": False, "version": version_of(old_text), "changes": 0}
    return _commit(name, path, doc, old_text, "edit", changes)


def _set_at(doc, path, value):
    """Set doc[path...] = value on a deep copy; the parents must already exist (a new LEAF
    key in an existing object is fine). Returns (new_doc, error)."""
    if not isinstance(path, list) or not path or not all(isinstance(p, (str, int)) and not isinstance(p, bool) for p in path):
        return None, "A setting path is a list of keys and positions."
    new = copy.deepcopy(doc)
    node = new
    for depth, step in enumerate(path[:-1]):
        if isinstance(node, dict) and isinstance(step, str) and step not in node:
            # A section that has never been written (a levels block in a fresh notify
            # config) is created, so every setting on the screen can be set the first time.
            node[step] = {}
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            return None, "There is no %s in this file." % _fmt_path(tuple(path[:depth + 1]))
    last = path[-1]
    if isinstance(node, dict) and isinstance(last, str):
        node[last] = value
    elif isinstance(node, list) and isinstance(last, int) and 0 <= last < len(node):
        node[last] = value
    else:
        return None, "There is no %s in this file." % _fmt_path(tuple(path))
    return new, None


def set_value(name, path, value, base_version, confirm_guarded=False):
    """Change ONE value (the Settings sections use this): same gate and checks as write()."""
    info, fpath, refusal = _gate(name, confirm_guarded)
    if refusal:
        return refusal
    old_text, err = _raw(fpath)
    if err:
        return {"ok": False, "error": err}
    if base_version is not None and base_version != version_of(old_text):
        return {"ok": False, "code": "stale", "version": version_of(old_text),
                "error": "The file changed on disk after you opened it. Reload, then try again."}
    old_doc, perr = _parse(old_text) if old_text is not None else ({}, None)
    if perr or not isinstance(old_doc, (dict, list)):
        return {"ok": False, "code": "invalid", "error": perr or "This file's top level is not an object."}
    new_doc, serr = _set_at(old_doc, path, value)
    if serr:
        return {"ok": False, "code": "bad_path", "error": serr}
    changes = diff(old_doc, new_doc, name)
    problems = validate(name, new_doc, old_doc)
    if problems:
        return {"ok": False, "code": "invalid", "problems": problems,
                "error": "Not saved: " + " ".join(problems)}
    if new_doc == old_doc:
        return {"ok": True, "changed": False, "version": version_of(old_text), "changes": 0}
    return _commit(name, fpath, new_doc, old_text, "set", changes)


def restore(name, backup_id, base_version, confirm_guarded=False):
    """Put a backup back. The file as it is now is backed up first, so a restore can itself
    be undone. `backup_id` None means the newest backup (the one-click Undo)."""
    info, path, refusal = _gate(name, confirm_guarded)
    if refusal:
        return refusal
    folder = _backup_dir(name)
    if backup_id is None:
        ids = sorted((p.stem for p in folder.glob("*.json") if _BACKUP_ID_RE.match(p.stem)), reverse=True) \
            if folder.is_dir() else []
        if not ids:
            return {"ok": False, "code": "no_backup", "error": "There is nothing to undo for this file yet."}
        backup_id = ids[0]
    if not isinstance(backup_id, str) or not _BACKUP_ID_RE.match(backup_id):
        return {"ok": False, "code": "bad_backup", "error": "That is not a backup of this file."}
    source = folder / (backup_id + ".json")
    try:
        text = source.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "code": "bad_backup", "error": "That backup is gone or unreadable."}
    doc, perr = _parse(text)
    if perr:
        return {"ok": False, "code": "bad_backup", "error": "That backup is not valid JSON, so it was not restored."}
    old_text, err = _raw(path)
    if err:
        return {"ok": False, "error": err}
    if base_version is not None and base_version != version_of(old_text):
        return {"ok": False, "code": "stale", "version": version_of(old_text),
                "error": "The file changed on disk after you opened it. Reload, then try again."}
    old_doc, _ = _parse(old_text) if old_text is not None else (None, None)
    changes = diff(old_doc if old_doc is not None else {}, doc, name)
    if old_text is not None and doc == old_doc:
        return {"ok": True, "changed": False, "version": version_of(old_text), "changes": 0, "restored": backup_id}
    result = _commit(name, path, doc, old_text, "restore", changes)
    if result.get("ok"):
        result["restored"] = backup_id
    return result


# --- tunables (layer 2) ----------------------------------------------------------------
def tunable_set(name, value):
    why = refuse_unattended()
    if why:
        return {"ok": False, "error": why, "code": "unattended"}
    before = tunables.stored(name)
    ok, result = tunables.set_override(name, value)
    if not ok:
        return {"ok": False, "error": result, "code": "invalid"}
    _log_change("tunables.json", "tunable-set", [name])
    return {"ok": True, "name": name, "value": result, "was": before}


def tunable_reset(name):
    why = refuse_unattended()
    if why:
        return {"ok": False, "error": why, "code": "unattended"}
    ok, result = tunables.reset_override(name)
    if not ok:
        return {"ok": False, "error": result, "code": "invalid"}
    _log_change("tunables.json", "tunable-reset", [name])
    return {"ok": True, "name": name}


def recent_changes(limit=50):
    log = _dir() / BACKUP_DIR_NAME / CHANGE_LOG_NAME
    try:
        lines = log.read_text(encoding=ENCODING).splitlines()[-int(limit):]
    except (OSError, UnicodeDecodeError, ValueError):
        return {"ok": True, "changes": []}
    rows = []
    for line in reversed(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return {"ok": True, "changes": rows}
