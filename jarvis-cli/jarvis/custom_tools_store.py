"""User-authored tools, stored in ~/.jarvis/tools/ — editable from the web UI.

WHY NOT JUST actions/
---------------------
jarvis/actions/*.py is already a drop-in tool directory, and it is the right
place for tools that ship WITH jarvis. It is the wrong place for a user's
own, for one blunt reason:

    script.bat reinstalls the package. Anything under jarvis/actions/ that
    the user wrote is gone.

That is a data-loss bug waiting to happen, and it is not hypothetical — the
whole reason ~/.jarvis exists, and the reason script.bat's own comments
promise never to rename it, is that config must outlive installs. A user's
custom tool is their work, not part of the install, so it lives beside their
commands, memory and skills, and is picked up from there.

Everything else is identical: the same TOOL_SCHEMAS/TOOLS/TOOL_GROUP contract
from actions/_template.py, validated by the same tool_loader.discover_actions(),
so a tool written here can be moved into actions/ verbatim if it ever
graduates into something worth shipping.

WHAT THIS ADDS ON TOP
---------------------
1. **Persistence in the right place** — ~/.jarvis/tools/<name>.py.
2. **Enable/disable without deleting** — a `.disabled` suffix, so switching
   a misbehaving tool off doesn't mean losing it.
3. **Management API** — list/read/write/delete/validate/test, which is what
   the Custom Tools tab drives. Editing Python in a browser textarea is only
   reasonable if saving something broken tells you *why* immediately, rather
   than failing silently at the next startup (which is exactly how a
   rejected action file behaves today — see actions/_template.py's warning
   about that).
4. **Templates** — a starter for each shape of tool, including ones that
   drive the UI bridge, because "what can a tool even do?" is the real
   barrier, not Python syntax.

THE SAFETY POSITION
-------------------
A tool here is arbitrary Python running in the jarvis process. That is not
a loophole — it is the same trust level as commands.json (which runs
arbitrary shell) and as editing the install directory. The person writing it
is the person running it.

What this module does NOT do is let the *model* write one. There is no
tool_create_custom_tool. The model can call custom tools, and it can read
them; authoring goes through the human at the keyboard, via the CLI or the
web editor. A model that could write its own tools could route around every
confirm gate in tool_safety.py by writing an unflagged tool that does the
same thing — so that door stays shut.
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
TOOLS_DIR = JARVIS_DIR / "tools"
META_FILE = TOOLS_DIR / "_meta.json"
ENCODING = "utf-8"

# Same shape as a tool name, because the filename becomes the module name.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
DISABLED_SUFFIX = ".disabled"
MAX_SOURCE_CHARS = 120_000


def ensure_dir():
    """Create the directory and drop a README explaining the contract.

    The README matters: someone who finds this folder in six months has no
    other clue what a file in it is supposed to look like, and the template
    lives in the install directory they may well have rebuilt since.
    """
    try:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        readme = TOOLS_DIR / "README.md"
        if not readme.exists():
            readme.write_text(_README, encoding=ENCODING)
    except OSError:
        pass
    return TOOLS_DIR


def _meta():
    try:
        data = json.loads(META_FILE.read_text(encoding=ENCODING))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}


def _save_meta(data):
    from . import atomic_io
    ensure_dir()
    return atomic_io.write_json(META_FILE, data)


def _path_for(name, disabled=False):
    return TOOLS_DIR / ("%s.py%s" % (name, DISABLED_SUFFIX if disabled else ""))


def _resolve(name):
    """Find a tool file whether it's currently enabled or disabled."""
    enabled = _path_for(name)
    if enabled.exists():
        return enabled, True
    disabled = _path_for(name, disabled=True)
    if disabled.exists():
        return disabled, False
    return None, False


def valid_name(name):
    return bool(_NAME_RE.match((name or "").strip()))


# ---------------------------------------------------------------------------
# Listing / reading / writing
# ---------------------------------------------------------------------------


def list_tools(include_source=False):
    """Every custom tool file with its validation status.

    Validation runs on every list rather than being cached, because the
    whole point of this surface is telling someone their file is broken —
    a cached "ok" from before they edited it would be worse than nothing.
    """
    ensure_dir()
    meta = _meta()
    out = []
    try:
        files = sorted(TOOLS_DIR.glob("*.py")) + sorted(TOOLS_DIR.glob("*.py" + DISABLED_SUFFIX))
    except OSError:
        return out

    for path in files:
        if path.name.startswith("_"):
            continue
        enabled = not path.name.endswith(DISABLED_SUFFIX)
        name = path.name[:-3] if enabled else path.name[:-(3 + len(DISABLED_SUFFIX))]
        entry = {
            "name": name,
            "enabled": enabled,
            "file": path.name,
            "size": _safe_size(path),
            "updated": meta.get(name, {}).get("updated") or _mtime(path),
            "description": meta.get(name, {}).get("description", ""),
        }
        check = validate_source(_read(path), name)
        entry["valid"] = check["ok"]
        entry["error"] = check.get("error", "")
        entry["tools"] = check.get("tools", [])
        entry["group"] = check.get("group", "")
        if include_source:
            entry["source"] = _read(path)
        out.append(entry)
    return out


def _safe_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0).isoformat()
    except OSError:
        return ""


def _read(path):
    try:
        return path.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError):
        return ""


def read_tool(name):
    path, enabled = _resolve(name)
    if not path:
        return {"ok": False, "error": "no custom tool named %r" % name}
    source = _read(path)
    check = validate_source(source, name)
    return {"ok": True, "name": name, "enabled": enabled, "source": source,
            "valid": check["ok"], "error": check.get("error", ""),
            "tools": check.get("tools", []), "group": check.get("group", "")}


def write_tool(name, source, description=""):
    """Create or overwrite. Validates BEFORE writing, and keeps a backup.

    Validating first is the important half: the editor's Save button should
    refuse to persist a file that would be silently rejected at startup. The
    backup is the other half — a browser textarea is an easy place to lose
    work, and one `.bak` costs nothing.
    """
    name = (name or "").strip().lower()
    if not valid_name(name):
        return {"ok": False,
                "error": "name must be lower_snake_case, start with a letter, "
                         "max 49 characters"}
    source = source or ""
    if len(source) > MAX_SOURCE_CHARS:
        return {"ok": False, "error": "source is too large (max %d characters)"
                                      % MAX_SOURCE_CHARS}

    check = validate_source(source, name)
    if not check["ok"]:
        return {"ok": False, "error": check["error"], "stage": check.get("stage", "validate")}

    ensure_dir()
    existing, was_enabled = _resolve(name)
    target = existing if existing else _path_for(name)
    try:
        if existing and existing.exists():
            shutil.copyfile(existing, existing.with_suffix(existing.suffix + ".bak"))
        target.write_text(source, encoding=ENCODING)
    except OSError as exc:
        return {"ok": False, "error": "couldn't write the file: %s" % exc}

    meta = _meta()
    meta[name] = {"updated": datetime.now().replace(microsecond=0).isoformat(),
                  "description": (description or "")[:200]}
    _save_meta(meta)

    return {"ok": True, "name": name, "enabled": was_enabled if existing else True,
            "path": str(target), "tools": check.get("tools", []),
            "group": check.get("group", ""),
            "checklist": check.get("checklist", []),
            "checklist_missing": check.get("checklist_missing", []),
            "checklist_problems": check.get("checklist_problems", []),
            "note": "Restart any running daemon (or just run the next command) "
                    "to pick it up — tools are discovered at process start."}


def delete_tool(name):
    path, _enabled = _resolve(name)
    if not path:
        return {"ok": False, "error": "no custom tool named %r" % name}
    try:
        path.unlink()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    meta = _meta()
    meta.pop(name, None)
    _save_meta(meta)
    return {"ok": True, "name": name}


def set_enabled(name, enabled):
    """Rename between `x.py` and `x.py.disabled`.

    A rename rather than a flag in _meta.json, because the loader scans the
    directory — a metadata flag would need the loader to read metadata, and
    a file the loader can't see is a much more certain kind of "off".
    """
    path, currently = _resolve(name)
    if not path:
        return {"ok": False, "error": "no custom tool named %r" % name}
    if bool(enabled) == currently:
        return {"ok": True, "name": name, "enabled": currently, "note": "already set"}
    target = _path_for(name, disabled=not enabled)
    try:
        path.rename(target)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": name, "enabled": bool(enabled)}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_source(source, name="custom_tool"):
    """Compile and inspect without registering anything.

    Three stages, each reporting separately, because "syntax error on line
    12" and "your TOOLS dict is missing a handler" need completely different
    fixes and a single "invalid" would make the user guess which they hit.

    Executing the module is unavoidable — TOOL_SCHEMAS is a module-level
    value, so there is no way to read it without running the top level. That
    is the same exposure tool_loader already accepts for actions/, and the
    person whose file it is is the person who asked to save it.
    """
    source = source or ""
    if not source.strip():
        return {"ok": False, "stage": "empty", "error": "the file is empty"}

    try:
        compile(source, "<%s>" % name, "exec")
    except SyntaxError as exc:
        return {"ok": False, "stage": "syntax",
                "error": "line %s: %s" % (exc.lineno, exc.msg)}

    import types
    module = types.ModuleType("jarvis_custom_%s" % name)
    module.__dict__["__file__"] = str(_path_for(name))
    try:
        exec(compile(source, "<%s>" % name, "exec"), module.__dict__)
    except Exception as exc:  # noqa: BLE001 — any import-time failure is a finding
        return {"ok": False, "stage": "import",
                "error": "%s: %s" % (type(exc).__name__, exc),
                "hint": "Module-level imports of ai_client/tool_router/tool_registry "
                        "hit a circular import — import them inside your handler "
                        "instead (see actions/_template.py)."}

    schemas = getattr(module, "TOOL_SCHEMAS", None)
    tools = getattr(module, "TOOLS", None)
    group = getattr(module, "TOOL_GROUP", None)

    if schemas is None and tools is None and group is None:
        return {"ok": False, "stage": "contract",
                "error": "no TOOL_SCHEMAS / TOOLS / TOOL_GROUP — this file defines "
                         "no tools",
                "hint": "Start from a template; all three are required."}
    if not isinstance(schemas, list) or not schemas:
        return {"ok": False, "stage": "contract",
                "error": "TOOL_SCHEMAS must be a non-empty list"}
    if not isinstance(tools, dict) or not tools:
        return {"ok": False, "stage": "contract",
                "error": "TOOLS must be a non-empty dict of name -> function"}
    if not isinstance(group, str) or not group.strip():
        return {"ok": False, "stage": "contract",
                "error": "TOOL_GROUP must be a non-empty string (an existing "
                         "router group, or a new one)"}

    names = []
    for schema in schemas:
        if not isinstance(schema, dict):
            return {"ok": False, "stage": "contract",
                    "error": "every TOOL_SCHEMAS entry must be a dict"}
        tool_name = schema.get("name")
        if not isinstance(tool_name, str) or not _NAME_RE.match(tool_name):
            return {"ok": False, "stage": "contract",
                    "error": "schema name %r must be lower_snake_case and start "
                             "with a letter" % tool_name}
        if not (schema.get("description") or "").strip():
            return {"ok": False, "stage": "contract",
                    "error": "schema %r needs a description — it's what tells the "
                             "model when to use it" % tool_name}
        if not isinstance(schema.get("parameters"), dict):
            return {"ok": False, "stage": "contract",
                    "error": "schema %r needs a parameters object (use "
                             '{"type":"object","properties":{},"required":[]} '
                             "if it takes none)" % tool_name}
        if tool_name not in tools or not callable(tools[tool_name]):
            return {"ok": False, "stage": "contract",
                    "error": "TOOLS[%r] is missing or isn't callable" % tool_name}
        names.append(tool_name)

    extra = set(tools) - set(names)
    if extra:
        return {"ok": False, "stage": "contract",
                "error": "TOOLS has handler(s) with no matching schema: %s"
                         % ", ".join(sorted(extra))}

    # Collide with a built-in and the loader will reject the whole file at
    # startup — better to say so now, while the editor is still open.
    clash = _builtin_clash(names)
    if clash:
        return {"ok": False, "stage": "contract",
                "error": "%s already exists as a built-in tool — pick another name"
                         % ", ".join(clash)}

    # G.1: the file's own Test Checklist entries. Never a reason to fail the
    # check (a malformed entry is dropped at discovery, the tool still loads —
    # see tool_loader.py), but the ONLY place a custom-tool author gets told:
    # discovery's log line goes to stderr, where nobody is looking.
    from . import checklist_schema
    entries, _group_meta, checklist_problems = checklist_schema.extract_supplied(
        getattr(module, "TEST_CHECKLIST", None),
        getattr(module, "TEST_CHECKLIST_GROUP", None),
        set(names), group.strip(),
    )

    return {"ok": True, "tools": names, "group": group.strip(),
            "keywords": getattr(module, "TOOL_KEYWORDS", {}) or {},
            "checklist": sorted(entries),
            "checklist_missing": [n for n in names if n not in entries],
            "checklist_problems": checklist_problems}


def _builtin_clash(names):
    """Names already taken by a BUILT-IN or a shipped actions/ tool.

    Names contributed by the user directory are excluded deliberately. Once a
    custom tool is saved it is part of the live catalog, so a naive
    "is this name in tools.TOOLS?" check finds the file's own name and
    reports it as clashing with itself — the tool would save cleanly once and
    then show as broken on every subsequent open, with an error message
    ("disk_report already exists as a built-in tool") that is actively
    misleading.

    A genuine collision between two USER files is still caught, just
    somewhere better: tool_loader rejects the second one at discovery with a
    message naming both files.
    """
    try:
        from . import tools as system_tools
        existing = set(system_tools.TOOLS or {})
        existing -= set(getattr(system_tools, "USER_TOOL_NAMES", set()) or set())
    except Exception:  # noqa: BLE001 — during partial init, skip the check
        return []
    return [n for n in names if n in existing]


def test_tool(name, tool_name=None, arguments=None):
    """Actually run one handler with given arguments and return its result.

    This is the "does it work" button. It runs the real handler — including
    any side effect it has — which is why the UI labels it Run rather than
    Test and shows the arguments first.
    """
    entry = read_tool(name)
    if not entry.get("ok"):
        return entry
    if not entry.get("valid"):
        return {"ok": False, "error": entry.get("error") or "this tool doesn't load"}

    import types
    module = types.ModuleType("jarvis_custom_test_%s" % name)
    try:
        exec(compile(entry["source"], "<%s>" % name, "exec"), module.__dict__)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "import failed: %s" % exc}

    handlers = getattr(module, "TOOLS", {}) or {}
    target = tool_name or (entry.get("tools") or [None])[0]
    handler = handlers.get(target)
    if not callable(handler):
        return {"ok": False, "error": "no handler named %r in this file" % target}

    try:
        import inspect
        params = inspect.signature(handler).parameters
        result = handler(arguments or {}, None) if len(params) >= 2 else handler(arguments or {})
    except Exception as exc:  # noqa: BLE001 — a throwing tool is the finding
        return {"ok": False, "tool": target,
                "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "tool": target, "result": result}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = {
    "minimal": {
        "label": "Minimal",
        "hint": "The smallest thing that works — one tool, one argument.",
        "source": '''"""My custom tool."""


def tool_hello(args):
    args = args or {}
    who = (args.get("name") or "world").strip()
    return {"ok": True, "greeting": "Hello, %s!" % who}


TOOL_SCHEMAS = [
    {
        "name": "hello",
        "description": "Say hello to someone. Use when the user asks to be greeted.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Who to greet."},
            },
            "required": [],
        },
    },
]

TOOLS = {"hello": tool_hello}
TOOL_GROUP = "custom"
TOOL_KEYWORDS = {"hello": {"say hello": 10, "greet": 8}}

# OPTIONAL, but worth the two minutes: this is what Menu > Test Checklist shows
# for the tool — how to try it and what a pass looks like. Without it the tool
# is listed there as "NO CHECKLIST". Keyed by tool name; "group" follows
# TOOL_GROUP, so leave it out. A step is EITHER {"ask": "<prompt to type into
# Ask>"} OR {"run": {<arguments>}} (run it straight from Debug, no model), plus
# "expect". Write <angle brackets> for things the tester fills in. Also allowed:
# "needs" (list), "os" ("windows"), "care" (a warning), "watch" (list).
TEST_CHECKLIST = {
    "hello": {
        "does": "Greets someone by name.",
        "steps": [
            {"ask": "Say hello to <a name>.",
             "expect": "Replies with 'Hello, <the name>!'."},
            {"run": {"name": "Ada"},
             "expect": "Returns ok: true and the greeting 'Hello, Ada!'."},
        ],
    },
}

# A brand-new TOOL_GROUP (anything other than "custom", or a shipped group such
# as "files") also wants a name for its section in that panel:
# TEST_CHECKLIST_GROUP = {"label": "My tools", "blurb": "What this group is for."}
''',
    },
    "ui": {
        "label": "Shows a popup",
        "hint": "Toasts, in-chat cards and modals via the UI bridge.",
        "source": '''"""A tool that talks back through the UI instead of just returning a dict."""


def tool_disk_report(args):
    # Imported inside the handler, not at module level — see
    # actions/_template.py's SAFE IMPORTS section.
    from jarvis import ui_bridge as ui
    import shutil

    args = args or {}
    path = (args.get("path") or "/").strip() or "/"
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        # A toast is the right shape for an error: transient, corner of the
        # screen, doesn't interrupt what the user was reading.
        ui.toast("Couldn't read %s: %s" % (path, exc), level="error")
        return {"ok": False, "error": str(exc)}

    used_pct = usage.used / usage.total * 100
    body = "Total  %.1f GB\\nUsed   %.1f GB (%.0f%%)\\nFree   %.1f GB" % (
        usage.total / 1e9, usage.used / 1e9, used_pct, usage.free / 1e9)

    # A bubble persists in the chat thread, so the user can scroll back to it.
    ui.bubble("Disk usage: %s" % path, body=body,
              level="warn" if used_pct > 90 else "info")

    # A modal interrupts — reserve it for things that genuinely should.
    if used_pct > 95:
        ui.dialog("Disk almost full", body, level="error")

    return {"ok": True, "path": path, "used_percent": round(used_pct, 1)}


TOOL_SCHEMAS = [
    {
        "name": "disk_report",
        "description": "Show a disk usage report as a card in the chat. Use when "
                       "the user asks how much disk space is left.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Drive or mount point."},
            },
            "required": [],
        },
    },
]

TOOLS = {"disk_report": tool_disk_report}
TOOL_GROUP = "custom"
TOOL_KEYWORDS = {"disk_report": {"disk space": 10, "how full": 8, "free space": 9}}

# What Menu > Test Checklist shows for this tool (see the minimal template for
# the field-by-field notes). Two steps: the path you'd normally use, and the
# error path — a toast is only worth having if you've seen it fire.
TEST_CHECKLIST = {
    "disk_report": {
        "does": "Shows disk usage as a card in the chat.",
        "steps": [
            {"ask": "How much disk space is left?",
             "expect": "A card appears in the chat with total, used and free space."},
            {"run": {"path": "<a folder that does not exist>"},
             "expect": "An error toast appears and the result is ok: false."},
        ],
    },
}
''',
    },
    "ask": {
        "label": "Asks the user something",
        "hint": "Blocking confirm / choose / form, with safe headless defaults.",
        "source": '''"""A tool that asks the user a question before it acts."""


def tool_cleanup(args):
    from jarvis import ui_bridge as ui
    from pathlib import Path

    args = args or {}
    folder = Path((args.get("folder") or ".").strip()).expanduser()
    if not folder.is_dir():
        ui.toast("Not a folder: %s" % folder, level="error")
        return {"ok": False, "error": "not a folder"}

    targets = sorted(folder.glob("*.tmp"))
    if not targets:
        ui.toast("Nothing to clean in %s" % folder, level="info")
        return {"ok": True, "deleted": 0}

    listing = "\\n".join(p.name for p in targets[:20])
    if len(targets) > 20:
        listing += "\\n… and %d more" % (len(targets) - 20)

    # EVERY blocking prompt needs a safe default, because this same tool may
    # run from a 3am scheduled job where nobody can answer. default=False
    # means "unattended runs delete nothing", which is the right way round.
    if not ui.confirm("Delete %d temp file(s)?" % len(targets),
                      body=listing, level="warn",
                      confirm_label="Delete", default=False):
        return {"ok": True, "deleted": 0, "note": "cancelled"}

    deleted = 0
    for target in targets:
        try:
            target.unlink()
            deleted += 1
        except OSError:
            pass

    ui.toast("Deleted %d file(s)" % deleted, level="success")
    return {"ok": True, "deleted": deleted}


TOOL_SCHEMAS = [
    {
        "name": "cleanup_temp",
        "description": "Delete .tmp files in a folder, after asking the user to "
                       "confirm. Use when the user asks to clean up temp files.",
        "parameters": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Folder to clean."},
            },
            "required": ["folder"],
        },
    },
]

TOOLS = {"cleanup_temp": tool_cleanup}
TOOL_GROUP = "custom"
TOOL_KEYWORDS = {"cleanup_temp": {"clean up": 9, "temp files": 10, "cleanup": 9}}

# What Menu > Test Checklist shows for this tool (see the minimal template for
# the field-by-field notes). "care" is the place to warn a tester about side
# effects before they run it — this one deletes files.
TEST_CHECKLIST = {
    "cleanup_temp": {
        "does": "Deletes .tmp files in a folder, after asking the user to confirm.",
        "steps": [
            {"ask": "Clean up the temp files in <a scratch folder holding a few .tmp files>.",
             "expect": "Asks to confirm and lists the files; No deletes nothing, Delete removes them."},
            {"run": {"folder": "<a scratch folder holding a few .tmp files>"},
             "expect": "The same confirmation; the result reports how many files were deleted."},
        ],
        "care": "Deletes real files - point it at a scratch folder, never a real one.",
    },
}

# This one deletes things, so it gets the real out-of-band gate as well —
# ui.confirm() above is a courtesy, this is the actual protection.
TOOL_CONFIRM_REQUIRED = {"cleanup_temp"}
''',
    },
    "http": {
        "label": "Calls an API",
        "hint": "Fetch something over HTTP and shape the result for the model.",
        "source": '''"""A tool that calls an HTTP API."""

DEFAULT_TIMEOUT = 10


def tool_api_status(args):
    import requests

    args = args or {}
    url = (args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "url must start with http:// or https://"}

    try:
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — never raise out of a tool
        return {"ok": False, "error": "request failed: %s" % exc}

    # Shape the result: the model pays for every character of this, so return
    # what it needs to answer, not the whole body.
    body = resp.text or ""
    return {
        "ok": resp.status_code < 400,
        "status": resp.status_code,
        "elapsed_ms": int(resp.elapsed.total_seconds() * 1000),
        "preview": body[:500],
        "truncated": len(body) > 500,
    }


TOOL_SCHEMAS = [
    {
        "name": "api_status",
        "description": "Check whether a URL responds, and how fast. Use when the "
                       "user asks if a site or API is up.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to check."},
            },
            "required": ["url"],
        },
    },
]

TOOLS = {"api_status": tool_api_status}
TOOL_GROUP = "custom"
TOOL_KEYWORDS = {"api_status": {"is it up": 10, "is down": 9, "check the api": 10}}

# What Menu > Test Checklist shows for this tool (see the minimal template for
# the field-by-field notes). "needs" lists what must exist before a test can pass.
TEST_CHECKLIST = {
    "api_status": {
        "does": "Checks whether a URL responds, and how fast.",
        "steps": [
            {"ask": "Is https://example.com up?",
             "expect": "Says it responds, with the status code and roughly how long it took."},
            {"run": {"url": "https://example.com"},
             "expect": "ok: true, a 200 status, elapsed_ms and a short preview of the body."},
            {"run": {"url": "not a url"},
             "expect": "ok: false with 'url must start with http:// or https://'."},
        ],
        "needs": ["Network access", "The requests package installed"],
    },
}
''',
    },
}


def templates():
    return [{"id": k, "label": v["label"], "hint": v["hint"]} for k, v in TEMPLATES.items()]


def template_source(template_id):
    entry = TEMPLATES.get(template_id) or TEMPLATES["minimal"]
    return entry["source"]


_README = """# Custom tools

Python files here become Jarvis tools. They live in `~/.jarvis/` on purpose:
rebuilding/reinstalling Jarvis wipes the install directory, and your tools
are yours, not part of the install.

Each file needs three module-level names:

    TOOL_SCHEMAS = [{"name": ..., "description": ..., "parameters": {...}}]
    TOOLS        = {"name": handler}          # handler(args: dict) -> dict
    TOOL_GROUP   = "custom"                   # router group

Optional:

    TOOL_KEYWORDS         = {"name": {"phrase": weight}}   # so it gets routed
    TOOL_PACK_INSTRUCTION = "one line of workflow guidance"
    TOOL_CONFIRM_REQUIRED = {"name"}          # gate it behind a confirmation
    TOOL_AI_REVIEW        = {"name"}          # and a second AI's risk check
    TEST_CHECKLIST        = {"name": {...}}   # how to test it, for Menu > Test Checklist
    TEST_CHECKLIST_GROUP  = {"label": "..."}  # names a brand-new TOOL_GROUP there

Rules that bite if you skip them:

* A handler must NEVER raise — catch and return `{"error": "..."}`.
* Import `ai_client`, `tool_router` or `tool_registry` INSIDE your handler,
  never at module level (circular import at discovery time).
* Without `TOOL_KEYWORDS`, a brand-new group is only reachable via
  `search_tools`, not by ordinary routing.
* Without `TEST_CHECKLIST`, Menu > Test Checklist lists your tool by name only,
  marked NO CHECKLIST. A malformed entry is dropped (the tool still loads) and
  the editor's Check button says why. Every template above carries an example.
* A file starting with `_` is ignored, so `_helpers.py` is safe to keep here.
* `x.py.disabled` is switched off but kept.

To show something on screen or ask a question:

    from jarvis import ui_bridge as ui
    ui.toast("done", level="success")
    if ui.confirm("Really?", default=False): ...

Manage these from the web UI: Menu > Custom Tools.
"""
