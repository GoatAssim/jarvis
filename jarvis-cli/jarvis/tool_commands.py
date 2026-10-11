"""TOOL_COMMANDS -- palette commands a tool file contributes (master plan I.2.11 / K.6.7).

WHY THIS EXISTS
---------------
The Ask box's `/` palette is a map of what jarvis-cli itself reserves. A tool you
drop into `~/.jarvis/tools/` (or `jarvis/actions/`) used to have no way to join it:
the only route to one of its tools was to ask the model, or open Debug. The owner
decided (D-I5 follow-on, K.6.7) that a tool should be able to put a command of its
own in the palette, with the same risk label and hover info the built-ins get.

    TOOL_COMMANDS = [
        {
            "name": "disk",                       # required: typed as /disk
            "tool": "disk_report",                # required: a tool of THIS file
            "summary": "How full is each drive",  # required: one line, shown in the list
            "riskTier": "safe",                   # "safe" | "caution" | "dangerous"
            "args": [                             # optional: typed words -> tool arguments
                {"name": "drive", "arg": "drive", "hint": "e.g. C:", "required": False},
            ],
            "fixed": {"verbose": False},          # optional: constants merged into the call
            "description": "Longer hover text.",  # optional
            "example": "/disk C:",                # optional
        },
    ]

WHAT A COMMAND IS (and is not)
------------------------------
A command is a typed twin of ONE tool of the file that declares it: `/disk C:` runs
`disk_report` with `{"drive": "C:", "verbose": false}`. It is not a way to run a
shell command, another file's tool, or an arbitrary argv. The command names the tool
at declaration time; what is typed after the name can only fill the declared `args`
(each one a string, coerced to the declared `type`), never choose the tool or add
keys. Same trust as TOOL_UI: the author of the file is the person who installed it,
and nothing model-facing can create or edit one (see custom_tools_store.py).

RISK LABEL -- FAIL CLOSED
-------------------------
`riskTier` reuses I.2.10's three tiers. A command that declares none, or declares
something that is not one of the three, is `dangerous` -- an unreviewed custom action
is an unknown quantity, so the palette asks before running it. That is the only
default here that is not the author's choice. (A bad value is also reported, so the
author learns why their `safe` command is asking first.)

NAMES
-----
`^[a-z][a-z0-9-]{1,31}$`. A name that is a CLI name jarvis reserves is dropped (the
built-in always wins, so a later jarvis release can't be shadowed by a tool); the first
registration of a name wins and a later duplicate is dropped. The palette also drops a
name that is one of its own verbs or aliases -- that list lives in the browser, so that
check is in `web/server.js` and `slash-palette.js`, not here.

Import-light on purpose: tool_loader.py imports this while tools.py is only half
initialised, so nothing here may import ai_client, tool_router, tool_registry or tools
at module level.
"""

import json
import re

RISK_TIERS = ("safe", "caution", "dangerous")
DEFAULT_TIER = "dangerous"          # fail closed: see the module docstring

TYPES = ("string", "number", "integer", "boolean")

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_ARG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

MAX_ENTRIES_PER_FILE = 12
MAX_ARGS = 6
MAX_SUMMARY = 120
MAX_DESCRIPTION = 400
MAX_HINT = 80
MAX_EXAMPLE = 120
MAX_FIXED_CHARS = 2000              # serialized size of `fixed`
MAX_ARG_CHARS = 2000                # one typed value (server.js applies the same cap)


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _reserved():
    """The CLI names jarvis reserves, or an empty set if they can't be read (a partial
    start-up). Imported lazily so this module stays import-light."""
    try:
        from . import reserved_names
        return set(getattr(reserved_names, "RESERVED_NAMES", ()) or ())
    except Exception:  # noqa: BLE001
        return set()


def _schema_properties(schemas, tool):
    """The property names a tool's schema declares, or None when we can't tell (no
    schema given, or one with no `properties` -- then nothing is checked)."""
    schema = (schemas or {}).get(tool)
    if not isinstance(schema, dict):
        return None
    params = schema.get("parameters")
    if not isinstance(params, dict):
        return None
    props = params.get("properties")
    return set(props) if isinstance(props, dict) and props else None


def _validate_args(raw, where, errors):
    """Returns the normalised arg list, or None if the whole entry should be dropped."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        errors.append("%s: args must be a list of dicts" % where)
        return None
    if len(raw) > MAX_ARGS:
        errors.append("%s: %d args; at most %d are allowed" % (where, len(raw), MAX_ARGS))
        return None
    out, seen_names, seen_keys = [], set(), set()
    for i, item in enumerate(raw):
        spot = "%s args[%d]" % (where, i)
        if not isinstance(item, dict):
            errors.append("%s is not a dict" % spot)
            return None
        name = item.get("name")
        if not isinstance(name, str) or not _ARG_RE.match(name):
            errors.append("%s: name %r must be a plain word (letters, digits, underscore)" % (spot, name))
            return None
        key = item.get("arg", name)
        if not isinstance(key, str) or not _ARG_RE.match(key):
            errors.append("%s: arg %r must be the tool's parameter name" % (spot, key))
            return None
        if name in seen_names or key in seen_keys:
            errors.append("%s: duplicate arg %r" % (spot, name))
            return None
        kind = item.get("type", "string")
        if kind not in TYPES:
            errors.append("%s: type must be one of %s, got %r" % (spot, " / ".join(TYPES), kind))
            return None
        rest = bool(item.get("rest", False))
        if rest and i != len(raw) - 1:
            errors.append("%s: only the LAST arg can be rest (it takes the remaining text)" % spot)
            return None
        if rest and kind != "string":
            errors.append("%s: a rest arg is the remaining text, so its type must be string" % spot)
            return None
        seen_names.add(name)
        seen_keys.add(key)
        out.append({
            "name": name, "arg": key, "type": kind,
            "required": bool(item.get("required", False)),
            "rest": rest,
            "hint": _text(item.get("hint"), MAX_HINT),
        })
    return out


def validate_commands(raw, filename, tool_names=None, schemas=None, reserved=None):
    """Validate a module's TOOL_COMMANDS. Returns (entries, errors).

    Never raises. A bad entry is dropped and described in `errors` (strings, for the
    loader's log and the Tool Manager editor's Check button); the rest stay, and the
    file's tools load regardless. `tool_names` is the set of tools the same file
    defines (a file with none can't have commands: they drive the file's own tools);
    `schemas` is {tool name: schema} for checking that an `arg` / `fixed` key is a real
    parameter of the tool; `reserved` overrides the reserved-name set (tests).
    """
    errors = []
    if raw is None:
        return [], errors
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return [], ["%s: TOOL_COMMANDS must be a list of dicts" % filename]
    tool_names = set(tool_names or ())
    if not tool_names:
        return [], ["%s: TOOL_COMMANDS is for a file that defines tools (a command runs one of them)" % filename]
    if len(raw) > MAX_ENTRIES_PER_FILE:
        errors.append("%s: TOOL_COMMANDS has %d entries; only the first %d are used"
                      % (filename, len(raw), MAX_ENTRIES_PER_FILE))
        raw = raw[:MAX_ENTRIES_PER_FILE]
    reserved = _reserved() if reserved is None else set(reserved)

    entries, seen = [], set()
    for index, item in enumerate(raw):
        where = "%s: TOOL_COMMANDS[%d]" % (filename, index)
        if not isinstance(item, dict):
            errors.append("%s is not a dict" % where)
            continue
        name = item.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append("%s: name %r must be lower-case words joined by '-', start with a letter, "
                          "2-32 characters (typed as /name)" % (where, name))
            continue
        where = "%s (%s)" % (where, name)
        if name in reserved:
            errors.append("%s: %r is a jarvis command name; the built-in wins, pick another" % (where, name))
            continue
        if name in seen:
            errors.append("%s: duplicate name in this file" % where)
            continue
        tool = item.get("tool")
        if not isinstance(tool, str) or tool not in tool_names:
            errors.append("%s: tool %r is not one of this file's tools (%s)"
                          % (where, tool, ", ".join(sorted(tool_names))))
            continue
        summary = _text(item.get("summary"), MAX_SUMMARY)
        if not summary:
            errors.append("%s: summary is required (one line, shown in the palette)" % where)
            continue

        # Fail closed: no tier, or an unknown one, is `dangerous`. Kept (not dropped) so
        # the command still works -- it just asks first -- and the author is told why.
        tier = item.get("riskTier")
        if tier is None:
            errors.append("%s: no riskTier declared; treated as 'dangerous' (asks before running). "
                          "Declare one of %s." % (where, " / ".join(RISK_TIERS)))
            tier = DEFAULT_TIER
        elif tier not in RISK_TIERS:
            errors.append("%s: riskTier %r is not one of %s; treated as 'dangerous'"
                          % (where, tier, " / ".join(RISK_TIERS)))
            tier = DEFAULT_TIER

        args = _validate_args(item.get("args"), where, errors)
        if args is None:
            continue

        fixed = item.get("fixed")
        if fixed is None:
            fixed = {}
        if not isinstance(fixed, dict) or any(not isinstance(k, str) or not _ARG_RE.match(k) for k in fixed):
            errors.append("%s: fixed must be a dict of parameter name -> JSON value" % where)
            continue
        try:
            fixed_text = json.dumps(fixed, sort_keys=True)
        except (TypeError, ValueError) as exc:
            errors.append("%s: fixed isn't plain JSON (%s)" % (where, exc))
            continue
        if len(fixed_text) > MAX_FIXED_CHARS:
            errors.append("%s: fixed is over %d characters" % (where, MAX_FIXED_CHARS))
            continue
        clash = sorted(set(fixed) & {a["arg"] for a in args})
        if clash:
            errors.append("%s: %s is both fixed and typed; a typed value can't override a fixed one"
                          % (where, ", ".join(clash)))
            continue
        props = _schema_properties(schemas, tool)
        if props is not None:
            unknown = sorted(({a["arg"] for a in args} | set(fixed)) - props)
            if unknown:
                errors.append("%s: %s isn't a parameter of %s (it takes: %s)"
                              % (where, ", ".join(unknown), tool, ", ".join(sorted(props))))
                continue

        seen.add(name)
        usage = " ".join(
            ("<%s>" % a["name"]) if a["required"] else ("[%s]" % a["name"]) for a in args)
        entries.append({
            "name": name,
            "tool": tool,
            "summary": summary,
            "riskTier": tier,
            "declaredTier": item.get("riskTier") if item.get("riskTier") in RISK_TIERS else "",
            "description": _text(item.get("description"), MAX_DESCRIPTION),
            "example": _text(item.get("example"), MAX_EXAMPLE),
            "usage": usage,
            "args": args,
            "fixed": json.loads(fixed_text),
            "file": filename,
        })
    return entries, errors


def public(entry, disabled=False, reason=""):
    """The shape the browser and `jarvis tool-commands list` see."""
    out = {k: v for k, v in entry.items() if not k.startswith("_")}
    out["disabled"] = bool(disabled)
    if disabled and reason:
        out["disabled_reason"] = reason
    return out


def _coerce(value, kind, name):
    """One typed word -> the declared type. Raises ValueError with a sentence."""
    if kind == "string":
        return value
    if kind == "boolean":
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        raise ValueError("%s must be yes/no (got %r)" % (name, value[:40]))
    try:
        if kind == "integer":
            return int(value.strip(), 10)
        number = float(value.strip())
    except ValueError:
        raise ValueError("%s must be a %s (got %r)" % (name, "whole number" if kind == "integer" else "number",
                                                        value[:40])) from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("%s must be a finite number" % name)
    return number


def build_arguments(entry, typed):
    """The tool's argument dict for what was typed after the command name.

    `typed` is the argv-style list of words (never a shell string). Returns
    (arguments, error); exactly one of them is None. Rules, in order: no more words
    than declared args (unless the last is `rest`, which takes the remainder joined by
    single spaces); every `required` arg present and non-empty; each value coerced to
    its declared type; `fixed` merged in last-but-never-over (validate_commands already
    refused a key that is both). Nothing typed can add a key that wasn't declared.
    """
    if not isinstance(typed, (list, tuple)) or any(not isinstance(t, str) for t in typed):
        return None, "arguments must be a list of strings"
    args = list(entry.get("args") or [])
    words = list(typed)
    if args and args[-1].get("rest") and len(words) > len(args):
        words = words[:len(args) - 1] + [" ".join(words[len(args) - 1:])]
    if len(words) > len(args):
        usage = entry.get("usage") or ""
        return None, "/%s takes %s%s" % (
            entry.get("name", ""), "no arguments" if not args else "at most %d" % len(args),
            (" (usage: /%s %s)" % (entry.get("name", ""), usage)) if usage else "")
    out = {}
    for i, spec in enumerate(args):
        value = words[i] if i < len(words) else ""
        if len(value) > MAX_ARG_CHARS or "\x00" in value:
            return None, "%s is too long or contains a NUL" % spec["name"]
        if not value.strip():
            if spec.get("required"):
                return None, "/%s needs %s" % (entry.get("name", ""), spec.get("hint") or "<%s>" % spec["name"])
            continue
        try:
            out[spec["arg"]] = _coerce(value, spec.get("type", "string"), spec["name"])
        except ValueError as exc:
            return None, str(exc)
    for key, value in (entry.get("fixed") or {}).items():
        out.setdefault(key, value)
    return out, None
