"""TOOL_UI -- a tool file's OWN screen: a button that opens its page, or a Menu entry
that opens its panel.

WHY THIS EXISTS
---------------
A tool used to have two ways to reach the person: return a dict to the model, or
call `ui_bridge` (toast / card / dialog / question) while it ran. Both are things
the tool says *while Jarvis is calling it*. Neither lets a tool have somewhere to
LIVE -- a dashboard, a form, a little control surface the owner opens on purpose.
The Test Checklist, Daemons and Backlog panels all work that way, and each took an
edit to index.html, app.js and style.css.

`TOOL_UI` lets a tool file ship that itself, without touching any of them:

    TOOL_UI = [
        {
            "id": "disk_dashboard",          # required, lower_snake, unique across Jarvis
            "label": "Disk",                 # required, the button / Menu entry text
            "mode": "button",                # required: "button" or "menu"
            "path": "disk_dashboard",        # required: folder, relative to THIS file
            "html": "tool.html",             # optional, these are the defaults
            "js":   "tool.js",
            "css":  "tool.css",
            "title": "Disk usage",           # optional, heading of the page / panel
            "hint": "How full is each drive", # optional, Menu entries only
            "icon": "D",                     # optional, 1-4 characters, buttons only
            "tool": "disk_report",           # optional, the tool of this file it belongs to
        },
    ]

THE TWO MODES
-------------
  button  A button in the bar beside the Menu button. Clicking it opens the tool's
          page in a window of its own (a sandboxed frame: the page can't reach the
          rest of Jarvis, only the small host API below).
  menu    An entry in the Menu, like Test Checklist or Daemons. Clicking it opens a
          panel and the tool's HTML goes into a div inside it, with the tool's CSS
          scoped to that div (a shadow root) so it can't restyle Jarvis and Jarvis's
          styles don't leak into it. The tool's JS runs against that div.

The tool picks the mode; the old way (`ui_bridge`) is untouched and still the right
choice for "say something while I run". Use TOOL_UI when the owner should be able
to open it themselves.

WHERE THE FILES LIVE
--------------------
Any folder under the file that declares them, however deep (`"path": "ui/disk"`).
`path` is REQUIRED -- the loader doesn't guess, and two tools can't both own a bare
`tool.html` next to their .py files. Each of `html` / `js` / `css` defaults to
`tool.html` / `tool.js` / `tool.css` inside that folder when present; name another
file to use it, or `""` to say "none". At least one has to exist.

WHAT IT MAY TOUCH (and what it can't)
-------------------------------------
Everything here is a lookup under the declaring file's own directory:
  * `path` and the file names are resolved and must stay INSIDE the declaring file's
    directory (no `..`, no absolute path, no symlink out). Anything else drops the
    entry with a message; the tool itself still loads.
  * only .html / .htm / .js / .css, each at most MAX_FILE_BYTES.
  * a UI element the owner switched off (Tool Manager, tool_disable.py) is neither
    listed to the browser nor served.

The JS is the tool author's code running in the owner's browser, exactly as the
tool's Python is the author's code running on the owner's machine -- same trust,
same reason: the person who wrote it is the person who installed it. There is no
model-facing way to create either (see custom_tools_store.py). In `button` mode the
sandbox makes that trust cheaper to extend; in `menu` mode the page and its script
share the document, so read a menu panel's JS before installing someone else's.

Import-light on purpose: tool_loader.py imports this while tools.py is only half
initialised, so nothing here may import ai_client, tool_router, tool_registry or
tools.
"""

import re
from pathlib import Path

MODES = ("button", "menu")
KINDS = ("html", "js", "css")
DEFAULT_FILES = {"html": "tool.html", "js": "tool.js", "css": "tool.css"}
_EXTENSIONS = {"html": (".html", ".htm"), "js": (".js",), "css": (".css",)}

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

MAX_FILE_BYTES = 400 * 1024
MAX_LABEL = 40
MAX_TITLE = 80
MAX_HINT = 140
MAX_ICON = 4
MAX_ENTRIES_PER_FILE = 12


def _clean_parts(raw):
    """Split a relative path into safe components, or None if it isn't one.

    Backslashes are accepted (a Windows author will type them) and normalised.
    Rejects empty, absolute, drive-lettered, and anything containing `..`.
    """
    text = str(raw if raw is not None else "").strip().replace("\\", "/")
    if not text or text.startswith("/") or _DRIVE_RE.match(text) or "\x00" in text:
        return None
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return parts


def _resolve_inside(base, parts):
    """`base`/parts, fully resolved (symlinks followed), or None if it lands outside
    `base`. The resolve-then-compare is the whole safety property: a lexical `..` check
    alone would let a symlink inside the folder point anywhere."""
    try:
        base = Path(base).resolve()
        target = base.joinpath(*parts).resolve()
        target.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return None
    return target


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def validate_ui(raw, filename, base_dir, tool_names=None):
    """Validate a module's TOOL_UI. Returns (entries, errors).

    Never raises. A bad entry is dropped and described in `errors` (strings, for
    the loader's log and the Tool Manager editor's Check button); the rest stay.
    `base_dir` is the directory of the declaring file (None => nothing can resolve,
    every entry is dropped with that reason). `tool_names` is the set of tools the
    same file defines, used only to check a `tool` field.
    """
    errors = []
    if raw is None:
        return [], errors
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return [], ["%s: TOOL_UI must be a list of dicts" % filename]
    if len(raw) > MAX_ENTRIES_PER_FILE:
        errors.append("%s: TOOL_UI has %d entries; only the first %d are used"
                      % (filename, len(raw), MAX_ENTRIES_PER_FILE))
        raw = raw[:MAX_ENTRIES_PER_FILE]
    tool_names = set(tool_names or ())

    entries, seen = [], set()
    for index, item in enumerate(raw):
        where = "%s: TOOL_UI[%d]" % (filename, index)
        if not isinstance(item, dict):
            errors.append("%s is not a dict" % where)
            continue
        uid = item.get("id")
        if not isinstance(uid, str) or not _ID_RE.match(uid):
            errors.append("%s: id %r must be lower_snake_case, start with a letter, 49 characters at most"
                          % (where, uid))
            continue
        where = "%s (%s)" % (where, uid)
        if uid in seen:
            errors.append("%s: duplicate id in this file" % where)
            continue
        label = _text(item.get("label"), MAX_LABEL)
        if not label:
            errors.append("%s: label is required" % where)
            continue
        mode = item.get("mode")
        if mode not in MODES:
            errors.append("%s: mode must be one of %s, got %r" % (where, " / ".join(MODES), mode))
            continue
        if base_dir is None:
            errors.append("%s: can't resolve files (this module has no location on disk)" % where)
            continue

        parts = _clean_parts(item.get("path"))
        if parts is None:
            errors.append("%s: path is required and must be a folder inside this file's directory "
                          "(no '..', no absolute path)" % where)
            continue
        folder = _resolve_inside(base_dir, parts)
        if folder is None:
            errors.append("%s: path %r leaves this file's directory" % (where, item.get("path")))
            continue

        tool = item.get("tool")
        if tool not in (None, ""):
            if not isinstance(tool, str) or (tool_names and tool not in tool_names):
                errors.append("%s: tool %r is not one of this file's tools" % (where, tool))
                continue
        tool = tool or ""

        files, missing, bad = {}, [], False
        for kind in KINDS:
            declared = item.get(kind)
            if declared is None:
                name, explicit = DEFAULT_FILES[kind], False
            elif declared == "" or declared is False:
                files[kind] = ""
                continue
            elif isinstance(declared, str):
                name, explicit = declared, True
            else:
                errors.append("%s: %s must be a file name, or \"\" for none" % (where, kind))
                bad = True
                break
            sub = _clean_parts(name)
            if sub is None or not str(name).lower().endswith(_EXTENSIONS[kind]):
                errors.append("%s: %s file %r must be a relative %s file name"
                              % (where, kind, name, "/".join(_EXTENSIONS[kind])))
                bad = True
                break
            target = _resolve_inside(folder, sub)
            if target is None:
                errors.append("%s: %s file %r leaves the folder" % (where, kind, name))
                bad = True
                break
            if target.is_file():
                files[kind] = "/".join(sub)
            else:
                files[kind] = ""
                if explicit:
                    missing.append(kind)
        if bad:
            continue
        if not any(files.get(k) for k in KINDS):
            errors.append("%s: no tool.html / tool.js / tool.css found in %r -- nothing to show"
                          % (where, "/".join(parts)))
            continue
        for kind in missing:
            errors.append("%s: %s file %r is declared but missing (the rest still loads)"
                          % (where, kind, item.get(kind)))

        seen.add(uid)
        entries.append({
            "id": uid,
            "label": label,
            "mode": mode,
            "title": _text(item.get("title"), MAX_TITLE) or label,
            "hint": _text(item.get("hint"), MAX_HINT),
            "icon": _text(item.get("icon"), MAX_ICON),
            "path": "/".join(parts),
            "files": files,
            "tool": tool,
            "tools": sorted(tool_names),
            "file": filename,
            "missing": missing,
            "_dir": str(folder),
        })
    return entries, errors


def public(entry, disabled=False):
    """The shape the browser and `jarvis tool-ui list` see: no absolute paths."""
    out = {k: v for k, v in entry.items() if not k.startswith("_")}
    out["disabled"] = bool(disabled)
    return out


def read_bundle(entry):
    """The text of an element's html / js / css, for the browser to assemble.

    Re-resolves every file at READ time rather than trusting the path computed at
    discovery: the file may have been swapped for a symlink or grown since. Never
    raises; a problem becomes `problems` and an empty string for that part.
    """
    out = {"ok": True, "id": entry.get("id", ""), "mode": entry.get("mode", ""),
           "title": entry.get("title", ""), "label": entry.get("label", ""),
           "tool": entry.get("tool", ""), "tools": list(entry.get("tools") or []),
           "html": "", "js": "", "css": "", "problems": []}
    folder = entry.get("_dir")
    if not folder:
        return {"ok": False, "error": "this element has no folder"}
    for kind in KINDS:
        name = (entry.get("files") or {}).get(kind) or ""
        if not name:
            continue
        sub = _clean_parts(name)
        target = _resolve_inside(folder, sub) if sub else None
        if target is None:
            out["problems"].append("%s file %r is no longer inside its folder" % (kind, name))
            continue
        try:
            if target.stat().st_size > MAX_FILE_BYTES:
                out["problems"].append("%s file %r is over %d KB" % (kind, name, MAX_FILE_BYTES // 1024))
                continue
            out[kind] = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            out["problems"].append("couldn't read %s file %r: %s" % (kind, name, exc))
    if not (out["html"] or out["js"] or out["css"]):
        out["ok"] = False
        out["error"] = "; ".join(out["problems"]) or "nothing to show"
    return out
