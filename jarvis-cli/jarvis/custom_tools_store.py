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
   the Tool Manager tab drives. Editing Python in a browser textarea is only
   reasonable if saving something broken tells you *why* immediately, rather
   than failing silently at the next startup (which is exactly how a
   rejected action file behaves today — see actions/_template.py's warning
   about that).
4. **Templates** — a starter for each shape of tool, including ones that
   drive the UI bridge, because "what can a tool even do?" is the real
   barrier, not Python syntax. Two of them ship a screen of their own
   (`TOOL_UI`, tool_ui.py): saving one also writes its starter html/js/css
   folder, and never overwrites a folder that already exists.
5. **Drafts** — an unfinished tool is backed up on disk as you type, saved or
   not (see DRAFTS below).
6. **A tool's web files** — the html/js/css folders its `TOOL_UI` declares can
   be edited next to the .py, imported together with it (several files, a
   folder or a .zip), and removed with it on uninstall (see WEB FILES below).

DRAFTS: UNFINISHED WORK IS BACKED UP WHETHER OR NOT YOU SAVED
-------------------------------------------------------------
`write_tool` refuses to persist a file that wouldn't load -- right, since a
rejected file silently never loads at the next start -- but that means the
half-written tool you were in the middle of exists ONLY in a browser tab. A
cleared browser, a different browser, a closed tab and a "Discard" each lose it.
So the editor syncs its text to `~/.jarvis/tools/_drafts/<key>.json` as you
type (a few seconds after you stop), and a Save the validator refuses stashes
the refused text there too, on the server, so even a client that never syncs
(`jarvis ctools-write`) can't lose it. A draft is a copy, never a tool: it is not
loaded, not listed as a tool, and not counted as a file the Tool Manager
manages. It is removed when the same text is saved successfully, kept when a tab
is closed with "Discard", and pruned after DRAFT_MAX_AGE_DAYS or past MAX_DRAFTS.
Each draft also keeps a few older snapshots (MAX_DRAFT_HISTORY), so an edit that
made things worse can be undone from the Tool Manager's "Unfinished tools" list.

WEB FILES: A TOOL'S SCREENS ARE EDITED, IMPORTED AND REMOVED WITH IT
--------------------------------------------------------------------
A tool that declares `TOOL_UI` owns folders of html/js/css (tool_ui.py). Three
things used to stop at the .py: the editor only opened that one file, import took
one file, and Delete left the folder behind. They now go through ONE rule: the
files a tool may touch are the ones its own saved `TOOL_UI` resolves to (or would
resolve to -- a `tool.css` it names but hasn't got yet), found by
`web_footprint()`. A caller never builds a path: it names a path, which must be in
that list, and the target is re-resolved inside ~/.jarvis/tools before anything is
read or written.
  * `read_web_file` / `write_web_file` -- the editor's extra tabs. One `.bak` of the
    previous text is kept, as for the .py; nothing here runs the file.
  * `import_bundle` -- a .py plus its folders, from several files, a folder or a
    .zip. A plan (no execution) first; `write=True` then checks the .py against the
    staged files exactly as a normal save would, writes only files a declared
    screen uses, never over an existing file unless `overwrite` is set (each replaced
    file then keeps a .bak), and rolls back if any .py is refused. A bundle may hold
    several top-level .py files: each is its own tool, all or none are written.
  * Drafts may carry the tab's unsaved screen-file text (`save_draft(..., web=...)`).
  * `delete_tool(name, remove_folders=True)` -- removes the files a screen serves
    (.html .htm .js .css and their .bak) and then the folder, if that leaves it empty.
    Anything else in the folder, a folder another tool also declares and a link are
    left alone and reported.
Like the .py itself, none of this has a model-facing entry point.

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
import time
from datetime import datetime
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
TOOLS_DIR = JARVIS_DIR / "tools"
META_FILE = TOOLS_DIR / "_meta.json"
DRAFTS_DIR = TOOLS_DIR / "_drafts"
ENCODING = "utf-8"

# Drafts (see the module docstring). The key is client-generated and becomes a
# file name, so it is validated before it touches a path.
DRAFT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_DRAFTS = 30
DRAFT_MAX_AGE_DAYS = 30
MAX_DRAFT_HISTORY = 5
# A new snapshot is pushed onto a draft's history at most this often, so typing
# for an hour doesn't turn five snapshots into five seconds of undo.
DRAFT_HISTORY_MIN_GAP = 120

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
        entry["ui"] = [u["id"] for u in check.get("ui", [])]
        entry["web_folders"] = web_footprint(check.get("ui"))["folders"]
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
            "tools": check.get("tools", []), "group": check.get("group", ""),
            "ui": check.get("ui", []), "ui_problems": check.get("ui_problems", []),
            "web": web_footprint(check.get("ui")),
            "commands": check.get("commands", []), "command_problems": check.get("command_problems", [])}


def write_tool(name, source, description="", draft_key="", scaffold=""):
    """Create or overwrite. Validates BEFORE writing, and keeps a backup.

    `draft_key` (the editor's tab key) and `scaffold` (a template id) are the two
    additions over the plain write:

      * a REFUSED save -- the source wouldn't load, wasn't a valid name, was too
        big -- is stashed as a draft before the refusal is returned, so the text
        is never only in a browser tab. With no `draft_key` the draft is keyed
        `failed-<name>`; a save that succeeds removes the draft it was made from.
        The result carries `draft_saved` either way.
      * `scaffold` names a template whose starter folder (html/js/css for a
        TOOL_UI) is written next to the file the first time, never over a folder
        that already exists. The result lists what was written in `scaffolded`.
    """
    result = _write_tool(name, source, description)
    key = (draft_key or "").strip().lower()
    if key and not DRAFT_KEY_RE.match(key):
        key = ""
    if not result.get("ok"):
        auto = key or ("failed-%s" % re.sub(r"[^a-z0-9_]", "", (name or "").strip().lower())[:49]).rstrip("-")
        if DRAFT_KEY_RE.match(auto or "") and (source or "").strip():
            saved = save_draft(auto, source, name=(name or "").strip().lower(),
                               mode="edit" if _resolve((name or "").strip().lower())[0] else "new",
                               reason="save refused: %s" % (result.get("error") or "unknown")[:160])
            result["draft_saved"] = bool(saved.get("ok") and not saved.get("skipped"))
            result["draft_key"] = auto
        return result
    if key:
        delete_draft(key)
    if scaffold:
        written = scaffold_template(scaffold, source)
        result["scaffolded"] = written["written"]
        if written.get("skipped"):
            result["scaffold_note"] = written["skipped"]
        if written["written"]:
            # The first validation ran before the files existed; report again.
            again = validate_source(source, result["name"])
            result["ui"] = again.get("ui", [])
            result["ui_problems"] = again.get("ui_problems", [])
    return result


def _write_tool(name, source, description=""):
    """The write itself (see write_tool). Validates BEFORE writing, and keeps a backup.

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
            "ui": check.get("ui", []),
            "ui_problems": check.get("ui_problems", []),
            "commands": check.get("commands", []),
            "command_problems": check.get("command_problems", []),
            "note": "Restart any running daemon (or just run the next command) "
                    "to pick it up — tools are discovered at process start."}


def delete_tool(name, remove_folders=False):
    """Remove a tool file. With `remove_folders`, also the screen files (and their
    folders) its TOOL_UI declared -- see WEB FILES in the module docstring for what
    is and isn't removed. Without it the folders stay, as they always did."""
    path, _enabled = _resolve(name)
    if not path:
        return {"ok": False, "error": "no custom tool named %r" % name}
    # Which tools the file provides, read BEFORE it goes, so their Tool Manager
    # on/off switches can be dropped with it (a new tool that reuses a name must
    # not inherit an old "off"). Same validation the list/show already ran.
    try:
        checked = validate_source(_read(path), name)
        provided = list(checked.get("tools") or [])
        provided_ui = [u["id"] for u in checked.get("ui", [])]
        provided_commands = [c["name"] for c in checked.get("commands", [])]
        ui_entries = list(checked.get("ui") or [])
    except Exception:  # noqa: BLE001 -- a file that won't import has nothing to forget
        provided, provided_ui, provided_commands, ui_entries = [], [], [], []
    plan = web_removal_plan(name, ui_entries) if remove_folders else []
    try:
        path.unlink()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    meta = _meta()
    meta.pop(name, None)
    _save_meta(meta)
    from . import tool_disable
    for tool_name in provided:
        tool_disable.forget_tool(tool_name)
    for ui_id in provided_ui:
        tool_disable.forget_ui(ui_id)
    for command_name in provided_commands:
        tool_disable.forget_tool_command(command_name)
    result = {"ok": True, "name": name}
    if remove_folders:
        result.update(_apply_removal_plan(plan))
    return result


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
# Drafts: unfinished work, backed up whether or not it was saved
# ---------------------------------------------------------------------------


def _draft_path(key):
    return DRAFTS_DIR / ("%s.json" % key)


def _read_draft_file(path):
    from . import atomic_io
    data = atomic_io.read_json(path, default=None, expect=dict)
    if not isinstance(data, dict) or not isinstance(data.get("source"), str):
        return None
    return data


def _clean_draft_web(web):
    """The screen-file text a draft carries: {relative path: text}, or (None, error).

    Only text for paths a tool screen could use (.html .htm .js .css, a safe relative path),
    each at most tool_ui.MAX_FILE_BYTES, at most IMPORT_MAX_WEB_FILES of them. This is a COPY
    kept in the drafts folder: nothing here is ever written under the tools folder or run.
    """
    from . import tool_ui
    if not isinstance(web, dict):
        return None, "screen files must be an object of path -> text"
    if len(web) > IMPORT_MAX_WEB_FILES:
        return None, "at most %d screen files can be backed up" % IMPORT_MAX_WEB_FILES
    out = {}
    for raw, text in web.items():
        parts = tool_ui._clean_parts(raw)
        if parts is None or not isinstance(text, str) or not parts[-1].lower().endswith(WEB_EXTENSIONS):
            return None, "%r is not a screen file path" % (raw,)
        if "\x00" in text or len(text.encode(ENCODING)) > tool_ui.MAX_FILE_BYTES:
            return None, "%s is not text, or is over %d KB" % ("/".join(parts), tool_ui.MAX_FILE_BYTES // 1024)
        out["/".join(parts)] = text
    return out, ""


def save_draft(key, source, name="", mode="new", orig_name="", template="", reason="", web=None):
    """Back up the text of an editor tab. Never raises; returns {"ok": ...}.

    Blank text is skipped (a draft of nothing is noise, and must not replace a real
    one). Unchanged text only refreshes nothing -- `unchanged` -- so a client that
    re-sends on every keystroke costs one comparison. The previous text goes onto a
    short history when it is at least DRAFT_HISTORY_MIN_GAP old, so the newest few
    versions survive a bad edit.

    `web` is the tab's unsaved screen-file text ({path: text}, see `_clean_draft_web`). Left
    out (None) it keeps whatever the draft already holds -- a refused save stashes only the
    .py and must not wipe the screens; an empty dict clears them. A draft with screen text
    is still only a copy; restoring it puts that text back in the editor's tabs, unsaved.
    """
    key = (key or "").strip().lower()
    if not DRAFT_KEY_RE.match(key):
        return {"ok": False, "error": "draft key must be lowercase letters, digits, - or _ (max 64)"}
    source = source if isinstance(source, str) else ""
    if not source.strip():
        return {"ok": True, "skipped": "empty"}
    if len(source) > MAX_SOURCE_CHARS:
        return {"ok": False, "error": "draft is too large (max %d characters)" % MAX_SOURCE_CHARS}
    from . import atomic_io
    now = time.time()
    existing = _read_draft_file(_draft_path(key))
    if web is None:
        kept = (existing or {}).get("web")
        web_clean = dict(kept) if isinstance(kept, dict) else {}
    else:
        web_clean, web_error = _clean_draft_web(web)
        if web_clean is None:
            return {"ok": False, "error": web_error}
    history = []
    if existing:
        if existing.get("source") == source and (existing.get("web") or {}) == web_clean:
            return {"ok": True, "unchanged": True, "key": key}
        history = [h for h in (existing.get("history") or [])
                   if isinstance(h, dict) and isinstance(h.get("source"), str)]
        last_push = history[-1].get("epoch", 0) if history else 0
        if existing.get("source", "").strip() and now - float(last_push or 0) >= DRAFT_HISTORY_MIN_GAP:
            history.append({"source": existing["source"], "saved": existing.get("saved", ""),
                            "epoch": float(existing.get("saved_epoch") or now)})
        history = history[-MAX_DRAFT_HISTORY:]
    record = {
        "key": key,
        "name": (name or "").strip().lower()[:49],
        "mode": "edit" if mode == "edit" else "new",
        "orig_name": (orig_name or "").strip().lower()[:49],
        "template": (template or "").strip()[:32],
        "reason": (reason or "")[:200],
        "source": source,
        "web": web_clean,
        "saved": datetime.now().replace(microsecond=0).isoformat(),
        "saved_epoch": now,
        "history": history,
    }
    try:
        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "couldn't create the drafts folder: %s" % exc}
    if not atomic_io.write_json(_draft_path(key), record):
        return {"ok": False, "error": "couldn't write the draft"}
    _prune_drafts()
    return {"ok": True, "key": key, "saved": record["saved"]}


def _draft_web_newer(web, saved_epoch):
    """True when a screen file the draft holds text for was saved on disk after the draft."""
    from . import tool_ui
    for rel in web or {}:
        parts = tool_ui._clean_parts(rel)
        target = tool_ui._resolve_inside(TOOLS_DIR, parts) if parts else None
        if target is not None and _mtime_epoch(target) > float(saved_epoch or 0) + 1:
            return True
    return False


def _draft_summary(data, include_source=False):
    source = data.get("source", "")
    web = data.get("web") if isinstance(data.get("web"), dict) else {}
    web = {k: v for k, v in web.items() if isinstance(k, str) and isinstance(v, str)}
    name = data.get("orig_name") or data.get("name") or ""
    on_disk, _enabled = _resolve(name) if name and valid_name(name) else (None, False)
    summary = {
        "key": data.get("key", ""),
        "name": data.get("name", ""),
        "mode": data.get("mode", "new"),
        "orig_name": data.get("orig_name", ""),
        "template": data.get("template", ""),
        "reason": data.get("reason", ""),
        "saved": data.get("saved", ""),
        "chars": len(source),
        "lines": source.count("\n") + 1,
        "preview": next((ln.strip() for ln in source.splitlines() if ln.strip()), "")[:120],
        "versions": len(data.get("history") or []),
        # The file this draft was taken from has been saved since: restoring would
        # replace newer text, so the panel says so before it does. Its screen files count too.
        "file_newer": bool(on_disk and _mtime_epoch(on_disk) > float(data.get("saved_epoch") or 0) + 1)
                      or _draft_web_newer(web, data.get("saved_epoch")),
        "web_files": sorted(web),
    }
    if include_source:
        summary["source"] = source
        summary["web"] = web
        summary["history"] = [{"saved": h.get("saved", ""), "source": h.get("source", "")}
                              for h in (data.get("history") or []) if isinstance(h, dict)]
    return summary


def _mtime_epoch(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def list_drafts():
    """Every backed-up draft, newest first, without the source text."""
    out = []
    try:
        files = list(DRAFTS_DIR.glob("*.json"))
    except OSError:
        return out
    for path in files:
        data = _read_draft_file(path)
        if data and DRAFT_KEY_RE.match(str(data.get("key", ""))):
            out.append(_draft_summary(data))
    out.sort(key=lambda d: d.get("saved", ""), reverse=True)
    return out


def read_draft(key):
    key = (key or "").strip().lower()
    if not DRAFT_KEY_RE.match(key):
        return {"ok": False, "error": "bad draft key"}
    data = _read_draft_file(_draft_path(key))
    if not data:
        return {"ok": False, "error": "no draft %r" % key}
    return dict(_draft_summary(data, include_source=True), ok=True)


def delete_draft(key):
    key = (key or "").strip().lower()
    if not DRAFT_KEY_RE.match(key):
        return {"ok": False, "error": "bad draft key"}
    path = _draft_path(key)
    removed = False
    # atomic_io keeps the previous version as <file>.bak and read_json falls back to it, so a
    # draft is only gone when the .bak (and any half-written .tmp) is gone too.
    for candidate in (path, path.with_suffix(path.suffix + ".bak"), path.with_suffix(path.suffix + ".tmp")):
        try:
            candidate.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    if not removed:
        return {"ok": True, "key": key, "note": "already gone"}
    return {"ok": True, "key": key}


def _prune_drafts():
    """Drop drafts past DRAFT_MAX_AGE_DAYS, then the oldest beyond MAX_DRAFTS.
    Best-effort: a draft that can't be removed is simply kept."""
    try:
        files = list(DRAFTS_DIR.glob("*.json"))
    except OSError:
        return
    cutoff = time.time() - DRAFT_MAX_AGE_DAYS * 86400
    kept = []
    for path in files:
        stamp = _mtime_epoch(path)
        if stamp and stamp < cutoff:
            try:
                path.unlink()
            except OSError:
                kept.append((stamp, path))
        else:
            kept.append((stamp, path))
    kept.sort(key=lambda item: item[0], reverse=True)
    for _stamp, path in kept[MAX_DRAFTS:]:
        try:
            path.unlink()
        except OSError:
            pass


def scaffold_template(template_id, source):
    """Write a template's starter folder next to the tool file, if it ships one.

    Only when the saved source still declares the template's folder (so a source
    that was rewritten to point elsewhere doesn't get a stray folder), and never
    over an existing folder: a second tool made from the same template keeps its
    own files rather than clobbering the first's. Returns
    {"written": [relative paths], "skipped": "reason" | ""}. Never raises.
    """
    entry = TEMPLATES.get(template_id)
    folder = (entry or {}).get("folder")
    files = (entry or {}).get("files")
    if not folder or not files:
        return {"written": [], "skipped": ""}
    if ('"%s"' % folder) not in (source or ""):
        return {"written": [], "skipped": ""}
    root = TOOLS_DIR / folder
    if root.exists():
        return {"written": [], "skipped": "The folder %s already exists, so its files were left alone." % folder}
    written = []
    try:
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding=ENCODING)
            written.append("%s/%s" % (folder, rel))
    except OSError as exc:
        return {"written": written, "skipped": "Couldn't write the starter files: %s" % exc}
    return {"written": written, "skipped": ""}


# ---------------------------------------------------------------------------
# A tool's web files: edit them, import a whole folder, remove them with the tool
# ---------------------------------------------------------------------------

WEB_EXTENSIONS = (".html", ".htm", ".js", ".css")
IMPORT_MAX_ENTRIES = 300              # files looked at in one import (a zip can hold anything)
IMPORT_MAX_BYTES = 8 * 1024 * 1024    # the whole bundle, uncompressed
IMPORT_MAX_WEB_FILES = 36             # 12 screens x html/js/css
IMPORT_MAX_TOOLS = 12                 # .py files (tools) in one import
IMPORT_READ_LINES = 200               # lines of each screen script the import review shows
IMPORT_READ_CHARS = 24000
_IMPORT_JUNK = {"thumbs.db", "desktop.ini"}


def web_footprint(ui_entries):
    """The screen files a tool owns, from its validated TOOL_UI entries (the public
    form `validate_source` returns).

        {"files":   [{"rel", "kind", "exists", "element", "label"}],   # rel is under ~/.jarvis/tools
         "folders": ["dice_page", ...]}

    Files a screen names (or defaults to) but doesn't have yet are listed with
    `exists: False` -- that is how the editor offers to create one. Never raises.
    """
    from . import tool_ui
    files, folders, seen = [], [], set()
    for entry in ui_entries or []:
        try:
            folder = tool_ui._clean_parts(entry.get("path"))
            if folder is None:
                continue
            joined = "/".join(folder)
            if joined not in folders:
                folders.append(joined)
            for item in tool_ui.element_files(entry, TOOLS_DIR):
                if item["rel"] in seen:
                    continue
                seen.add(item["rel"])
                item["element"] = entry.get("id", "")
                item["label"] = entry.get("label", "")
                files.append(item)
        except Exception:  # noqa: BLE001 -- a malformed entry must not break listing
            continue
    return {"files": files, "folders": folders}


def _web_ref(name, rel):
    """({"rel", "target", "kind", "exists"}, "") for one of the tool's OWN screen
    files, or (None, reason). The list comes from the tool's saved file on disk, so
    the browser names a path and this decides whether it is allowed."""
    from . import tool_ui
    name = (name or "").strip().lower()
    path, _enabled = _resolve(name)
    if not path:
        return None, "no custom tool named %r" % name
    check = validate_source(_read(path), name)
    if not check["ok"]:
        return None, "the saved file doesn't load, so its screen files can't be located: %s" % check["error"]
    parts = tool_ui._clean_parts(rel)
    if parts is None:
        return None, "%r is not a relative path inside the tools folder" % (rel,)
    clean = "/".join(parts)
    match = next((f for f in web_footprint(check.get("ui"))["files"] if f["rel"] == clean), None)
    if match is None:
        return None, "%s is not one of this tool's screen files" % clean
    target = tool_ui._resolve_inside(TOOLS_DIR, parts)
    if target is None:
        return None, "%s leaves the tools folder" % clean
    return {"rel": clean, "target": target, "kind": match["kind"], "exists": target.is_file()}, ""


def read_web_file(name, rel):
    """The text of one of a tool's screen files (the editor's extra tabs)."""
    from . import tool_ui
    ref, error = _web_ref(name, rel)
    if ref is None:
        return {"ok": False, "error": error}
    out = {"ok": True, "rel": ref["rel"], "kind": ref["kind"], "exists": ref["exists"], "text": "", "size": 0}
    if not ref["exists"]:
        return out
    try:
        size = ref["target"].stat().st_size
        if size > tool_ui.MAX_FILE_BYTES:
            return {"ok": False, "error": "%s is over %d KB, which is more than a screen can serve"
                                          % (ref["rel"], tool_ui.MAX_FILE_BYTES // 1024)}
        out["text"] = ref["target"].read_text(encoding=ENCODING)
        out["size"] = size
    except UnicodeDecodeError:
        return {"ok": False, "error": "%s isn't a UTF-8 text file" % ref["rel"]}
    except OSError as exc:
        return {"ok": False, "error": "couldn't read %s: %s" % (ref["rel"], exc)}
    return out


def _atomic_text(target, text):
    """Write beside the target and swap it in, so a crash never leaves half a file."""
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=ENCODING) as handle:
            handle.write(text)
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_web_file(name, rel, text):
    """Save one of a tool's screen files. Creates it if the screen names it but it
    doesn't exist yet; keeps one `.bak` of what it replaces. Nothing is run."""
    from . import tool_ui
    ref, error = _web_ref(name, rel)
    if ref is None:
        return {"ok": False, "error": error}
    text = text if isinstance(text, str) else ""
    if "\x00" in text:
        return {"ok": False, "error": "that doesn't look like a text file"}
    if len(text.encode(ENCODING)) > tool_ui.MAX_FILE_BYTES:
        return {"ok": False, "error": "%s would be over %d KB, which is more than a screen can serve"
                                      % (ref["rel"], tool_ui.MAX_FILE_BYTES // 1024)}
    target = ref["target"]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if ref["exists"]:
            shutil.copyfile(target, target.with_name(target.name + ".bak"))
        _atomic_text(target, text)
    except OSError as exc:
        return {"ok": False, "error": "couldn't write %s: %s" % (ref["rel"], exc)}
    return {"ok": True, "rel": ref["rel"], "kind": ref["kind"], "created": not ref["exists"],
            "size": len(text.encode(ENCODING))}


# ---- removal (uninstall) ----------------------------------------------------


def _is_web_name(filename):
    low = filename.lower()
    if low.endswith(".bak"):
        low = low[:-4]
    return low.endswith(WEB_EXTENSIONS)


def web_removal_plan(name, ui_entries):
    """What uninstalling `name` WITH its folders would remove and what it would leave.

    One dict per folder the tool's TOOL_UI declares and that still exists:
      {"path", "remove": [rel...], "keep": [rel...], "blocked": "reason" | ""}
    A folder is `blocked` (nothing in it is touched) when it is a link or sits under
    one, is reserved (a name starting with "_"), or another tool also uses it --
    judged by the elements other tools actually loaded AND by their source text, so a
    disabled or broken file that names the folder still protects it. Inside a folder
    only the files a screen serves (.html .htm .js .css and their .bak) are removed;
    anything else is `keep`, and the folder stays if anything is. Never raises.
    """
    import os
    from . import tool_ui
    plan = []
    try:
        root = TOOLS_DIR.resolve()
    except OSError:
        return plan
    mine = "%s.py" % name
    others_text = []
    try:
        for other in sorted(TOOLS_DIR.glob("*.py")) + sorted(TOOLS_DIR.glob("*.py" + DISABLED_SUFFIX)):
            if other.name.startswith("_") or other.name in (mine, mine + DISABLED_SUFFIX):
                continue
            others_text.append(_read(other).replace("\\", "/"))
    except OSError:
        pass
    loaded = []
    try:
        from . import tools as system_tools
        for u in getattr(system_tools, "AUTO_UI", []) or []:
            if u.get("file") != mine and u.get("_dir"):
                for fname in (u.get("files") or {}).values():
                    if fname:
                        loaded.append(Path(u["_dir"]) / fname)
    except Exception:  # noqa: BLE001 -- during partial init, rely on the text check
        pass

    seen = set()
    for entry in ui_entries or []:
        parts = tool_ui._clean_parts((entry or {}).get("path"))
        if parts is None or "/".join(parts) in seen:
            continue
        rel = "/".join(parts)
        seen.add(rel)
        folder = tool_ui._resolve_inside(root, parts)
        item = {"path": rel, "remove": [], "keep": [], "blocked": ""}
        if folder is None:
            item["blocked"] = "it is outside the tools folder"
        elif folder == root or parts[0].startswith("_"):
            item["blocked"] = "it is a reserved folder"
        elif any((root.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)):
            item["blocked"] = "it is a link, so it was left alone"
        elif not folder.is_dir():
            continue                                     # already gone: nothing to do
        else:
            # The folder as a quoted path ("ui", './ui/disk', "ui/disk/tool.js"), not as a
            # substring: "ui" is inside half the words in any Python file.
            named = re.compile(r"""["'](?:\./)?%s(?:/[^"']*)?["']""" % re.escape(rel), re.I)
            if any(named.search(text) for text in others_text):
                item["blocked"] = "another tool file also names it"
            else:
                for path in loaded:
                    try:
                        path.resolve().relative_to(folder)
                    except (OSError, ValueError, RuntimeError):
                        continue
                    item["blocked"] = "another tool's screen uses files in it"
                    break
        if not item["blocked"]:
            for dirpath, dirnames, filenames in os.walk(str(folder), followlinks=False):
                base = Path(dirpath)
                for d in list(dirnames):
                    if (base / d).is_symlink():
                        item["keep"].append("%s/%s" % (base.relative_to(root).as_posix(), d))
                        dirnames.remove(d)
                for fname in filenames:
                    full = base / fname
                    shown = full.relative_to(root).as_posix()
                    if full.is_symlink() or not _is_web_name(fname):
                        item["keep"].append(shown)
                    else:
                        item["remove"].append(shown)
        plan.append(item)
    return plan


def _apply_removal_plan(plan):
    """Carry out web_removal_plan. Returns what happened, for the caller to show:
    {"files_removed": [...], "folders_removed": [...], "folders_kept": [{"path", "why"}]}."""
    import os
    result = {"files_removed": [], "folders_removed": [], "folders_kept": []}
    try:
        root = TOOLS_DIR.resolve()
    except OSError:
        return result
    for item in plan:
        if item["blocked"]:
            result["folders_kept"].append({"path": item["path"], "why": item["blocked"]})
            continue
        for rel in item["remove"]:
            try:
                (root / rel).unlink()
                result["files_removed"].append(rel)
            except OSError:
                item["keep"].append(rel)
        folder = root.joinpath(*item["path"].split("/"))
        # Deepest first: a folder only goes if what was in it did.
        for dirpath, _dirs, _files in os.walk(str(folder), topdown=False, followlinks=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
        if folder.exists():
            result["folders_kept"].append({
                "path": item["path"],
                "why": "other files are still in it (%d)" % max(1, len(item["keep"]))})
        else:
            result["folders_removed"].append(item["path"])
    return result


def removal_preview(name):
    """`web_removal_plan` for a saved tool, for the confirmation before an uninstall."""
    path, _enabled = _resolve(name)
    if not path:
        return {"ok": False, "error": "no custom tool named %r" % name}
    check = validate_source(_read(path), name)
    return {"ok": True, "name": name, "folders": web_removal_plan(name, check.get("ui") if check["ok"] else [])}


# ---- import: a .py with its folders ----------------------------------------


def name_from_filename(filename):
    """"My Tool-v2.py" -> "my_tool_v2"; "" when nothing usable is left. The same
    rule as the Tool Manager's importNameFromFile."""
    base = re.sub(r"^.*[\\/]", "", str(filename or ""))
    base = re.sub(r"\.py(\.disabled)?$", "", base, flags=re.I)
    base = re.sub(r"[^a-z0-9_]+", "_", base.lower())
    base = re.sub(r"_+", "_", base).strip("_")
    if base and not re.match(r"[a-z]", base):
        base = "tool_" + base
    return base[:49]


def read_zip_bundle(zip_path):
    """[(path, bytes)] from a .zip. Raises ValueError with a sentence for the person.

    Sizes are checked from the headers first and again while reading (a header can
    lie), encrypted archives and link members are refused or skipped, and nothing is
    ever extracted to disk here: the members stay in memory until import_bundle has
    judged every path.
    """
    import stat
    import zipfile
    try:
        archive = zipfile.ZipFile(str(zip_path))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("that isn't a readable .zip file (%s)" % exc)
    items, total = [], 0
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > IMPORT_MAX_ENTRIES:
            raise ValueError("the zip holds %d files; at most %d are read" % (len(infos), IMPORT_MAX_ENTRIES))
        if sum(i.file_size for i in infos) > IMPORT_MAX_BYTES:
            raise ValueError("the zip is over %d MB uncompressed" % (IMPORT_MAX_BYTES // (1024 * 1024)))
        for info in infos:
            if info.flag_bits & 0x1:
                raise ValueError("the zip is password-protected")
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                continue
            try:
                with archive.open(info) as handle:
                    data = handle.read(IMPORT_MAX_BYTES + 1)
            except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                raise ValueError("couldn't read %s from the zip (%s)" % (info.filename, exc))
            total += len(data)
            if total > IMPORT_MAX_BYTES:
                raise ValueError("the zip is over %d MB uncompressed" % (IMPORT_MAX_BYTES // (1024 * 1024)))
            items.append((info.filename, data))
    return items


def _bundle_files(items):
    """Judge every path in a bundle. Returns (files, skipped, error):
    files = {"x.py": text, "ui/tool.html": text, ...} (one root folder stripped),
    skipped = [{"path", "why"}], error = "" or a sentence (an unsafe path refuses the
    whole bundle -- nothing partial is better than a half-trusted archive)."""
    from . import tool_ui
    cleaned, skipped = [], []
    for raw, data in items:
        text_path = str(raw if raw is not None else "").replace("\\", "/")
        parts = tool_ui._clean_parts(text_path)
        if parts is None:
            return {}, skipped, "unsafe path in the bundle: %r (no absolute paths, drive letters or '..')" % (raw,)
        cleaned.append((parts, data))
    if len(cleaned) > IMPORT_MAX_ENTRIES:
        return {}, skipped, "the bundle holds %d files; at most %d are read" % (len(cleaned), IMPORT_MAX_ENTRIES)
    # Hidden files and macOS/Python droppings are never part of a tool.
    kept = []
    for parts, data in cleaned:
        if any(p.startswith(".") or p.lower() in ("__macosx", "__pycache__") for p in parts) or parts[-1].lower() in _IMPORT_JUNK:
            continue
        kept.append((parts, data))
    # One wrapping folder (a picked folder, or a zip made from one) is not part of the layout.
    if kept and all(len(parts) > 1 for parts, _ in kept) and len({parts[0] for parts, _ in kept}) == 1:
        kept = [(parts[1:], data) for parts, data in kept]
    files = {}
    for parts, data in kept:
        rel = "/".join(parts)
        low = rel.lower()
        is_py = len(parts) == 1 and low.endswith(".py")
        if not (is_py or low.endswith(WEB_EXTENSIONS)):
            skipped.append({"path": rel, "why": "a tool doesn't use this kind of file"})
            continue
        if parts[0].startswith("_") and not is_py:
            skipped.append({"path": rel, "why": "folders starting with _ are reserved"})
            continue
        if isinstance(data, bytes):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                skipped.append({"path": rel, "why": "not a UTF-8 text file"})
                continue
        else:
            text = str(data)
        if "\x00" in text:
            skipped.append({"path": rel, "why": "not a text file"})
            continue
        if not is_py and len(text.encode(ENCODING)) > tool_ui.MAX_FILE_BYTES:
            skipped.append({"path": rel, "why": "over %d KB" % (tool_ui.MAX_FILE_BYTES // 1024)})
            continue
        files[rel] = text
    return files, skipped, ""


def _web_kind(rel):
    return "html" if rel.lower().endswith((".html", ".htm")) else rel.rsplit(".", 1)[-1].lower()


def _names_for(pys, name, names):
    """{py_file: final name}. `names` ({py_file: name}) wins; a lone `name` applies only
    when the bundle holds exactly one tool; everything else is named from its file."""
    given = {}
    if isinstance(names, dict):
        for key, value in names.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                given[key] = value.strip().lower()
    out = {}
    for rel in pys:
        if rel in given:
            out[rel] = given[rel]
        elif len(pys) == 1 and (name or "").strip():
            out[rel] = name.strip().lower()
        else:
            out[rel] = name_from_filename(rel)
    return out


def plan_import(items, name="", names=None, overwrite=False):
    """What importing this bundle would do, WITHOUT running anything.

    {"ok": True, "tools": [{"py_file", "name", "name_problem", "exists", "py_size", "py_lines",
                            "py_head"}],
     "name", "name_problem", "py_file", "py_size", "py_lines", "py_head",   # the first tool
     "web": [{"path", "size", "kind", "exists", ("lines", "head" for .js/.html)}], "skipped": [{"path", "why"}],
     "problems": [str], "collisions": {"tools": [name], "files": [path]},
     "overwrite": bool, "replaces": {"tools": [name], "files": [path]}}

    A bundle may hold several top-level .py files: each is its own tool, and the screen
    folders are shared. `name_problem` (a name that is invalid, taken or used twice) and
    `problems` (a screen file that is empty, too large, outside the tools folder, a link, or
    that already exists) are the blockers; names are kept apart because the person can change
    them and nothing else.

    `collisions` is what already exists and would be replaced. Without `overwrite` each one is
    also a blocker (as it always was). With `overwrite=True` they are not: they are listed in
    `replaces` instead, and import_bundle keeps one `.bak` of each file it replaces. A link or
    a folder at a screen file's path is a blocker either way -- nothing is written through one.

    Or {"ok": False, "error": ...} when the bundle can't be read at all."""
    from . import tool_ui
    files, skipped, error = _bundle_files(items)
    if error:
        return {"ok": False, "error": error}
    pys = sorted(rel for rel in files if "/" not in rel and rel.lower().endswith(".py"))
    helpers = [rel for rel in pys if rel.startswith("_")]
    for rel in helpers:
        skipped.append({"path": rel, "why": "helper modules (starting with _) aren't imported"})
    pys = [rel for rel in pys if rel not in helpers]
    for rel in helpers:
        files.pop(rel, None)
    if not pys:
        return {"ok": False, "error": "no .py tool file at the top of the folder or zip"
                                      " (a screen's files go in folders beside it)"}
    if len(pys) > IMPORT_MAX_TOOLS:
        return {"ok": False, "error": "found %d .py files (%s); at most %d tools are imported at once"
                                      % (len(pys), ", ".join(pys), IMPORT_MAX_TOOLS)}
    web = sorted(rel for rel in files if rel not in pys)
    if len(web) > IMPORT_MAX_WEB_FILES:
        return {"ok": False, "error": "%d screen files; at most %d are read" % (len(web), IMPORT_MAX_WEB_FILES)}
    overwrite = bool(overwrite)
    finals = _names_for(pys, name, names)
    problems, tools, collide_tools, collide_files = [], [], [], []
    seen_names = {}
    for py_file in pys:
        source = files[py_file]
        final = finals[py_file]
        problem, exists = "", False
        if not valid_name(final):
            problem = "%r isn't a usable tool name (lower_snake_case, start with a letter, 49 characters at most)" % final
        elif final in seen_names:
            problem = "%s and %s would both be saved as %s" % (seen_names[final], py_file, final)
        else:
            exists = bool(_resolve(final)[0])
            if exists:
                collide_tools.append(final)
                if not overwrite:
                    problem = "a tool file called %s already exists" % final
        if valid_name(final):
            seen_names.setdefault(final, py_file)
        if not source.strip():
            problems.append("%s is empty" % py_file)
        elif len(source) > MAX_SOURCE_CHARS:
            problems.append("%s is too large (max %d characters)" % (py_file, MAX_SOURCE_CHARS))
        tools.append({"py_file": py_file, "name": final, "name_problem": problem, "exists": exists,
                      "py_size": len(source.encode(ENCODING)), "py_lines": source.count("\n") + 1,
                      "py_head": "\n".join(source.split("\n")[:80])})
    web_rows = []
    for rel in web:
        parts = tool_ui._clean_parts(rel)
        target = tool_ui._resolve_inside(TOOLS_DIR, parts) if parts else None
        exists = False
        # A link anywhere on the way is named as a link FIRST: _resolve_inside follows it, so a link
        # that points out of the tools folder would otherwise read as a plain "outside the folder" path.
        linked = bool(parts) and any(TOOLS_DIR.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1))
        if linked:
            exists = True
            problems.append("%s is a link in the tools folder, and importing never writes through a link" % rel)
        elif target is None:
            problems.append("%s would land outside the tools folder" % rel)
        elif target.is_symlink():
            exists = True
            problems.append("%s is a link in the tools folder, and importing never writes through a link" % rel)
        elif target.is_dir():
            exists = True
            problems.append("%s is a folder in the tools folder, so a file can't be written there" % rel)
        elif target.exists():
            exists = True
            collide_files.append(rel)
            if not overwrite:
                problems.append("%s already exists, so importing would overwrite it" % rel)
        kind = _web_kind(rel)
        row = {"path": rel, "size": len(files[rel].encode(ENCODING)), "kind": kind, "exists": exists}
        if kind in ("js", "html"):
            # Script a screen runs in the page: the review shows it so it can be read first.
            lines = files[rel].split("\n")
            row["lines"] = len(lines)
            row["head"] = "\n".join(lines[:IMPORT_READ_LINES])[:IMPORT_READ_CHARS]
        web_rows.append(row)
    first = tools[0]
    return {"ok": True, "tools": tools,
            "name": first["name"], "name_problem": next((t["name_problem"] for t in tools if t["name_problem"]), ""),
            "py_file": first["py_file"], "py_size": first["py_size"], "py_lines": first["py_lines"],
            "py_head": first["py_head"],
            "web": web_rows, "skipped": skipped, "problems": problems,
            "collisions": {"tools": collide_tools, "files": collide_files},
            "overwrite": overwrite,
            "replaces": {"tools": collide_tools if overwrite else [], "files": collide_files if overwrite else []},
            "_files": files}


def _snapshot_tool(name):
    """What a tool file looked like before an import replaced it, so a failed import can put
    it back: None when there was no such tool."""
    path, enabled = _resolve(name)
    if not path:
        return None
    return {"path": path, "text": _read(path), "enabled": enabled, "meta": _meta().get(name)}


def _undo_tool(name, snap):
    """Reverse one tool write made by import_bundle. Best effort, never raises."""
    try:
        if snap is None:
            for path in (_path_for(name), _path_for(name, disabled=True)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        else:
            snap["path"].write_text(snap["text"], encoding=ENCODING)
        meta = _meta()
        if snap is None or snap.get("meta") is None:
            meta.pop(name, None)
        else:
            meta[name] = snap["meta"]
        _save_meta(meta)
    except OSError:
        pass


def import_bundle(items, name="", write=False, description="", names=None, overwrite=False):
    """Import one or more tools, each a .py plus the screen folders it declares: preview, or write.

    `items` is [(path, text-or-bytes)]. With `write=False` this is `plan_import`
    (minus the internal text). With `write=True` it:
      1. refuses on any blocker in the plan,
      2. checks every .py exactly as a normal save does, against a staging folder
         that holds the bundle's screen files (so what each declares is judged with its
         files), and refuses the whole bundle if any one is refused or two of them declare
         the same tool, screen or command name,
      3. keeps only the screen files a declared screen actually uses,
      4. writes them -- never over an existing file, unless `overwrite` is set, in which
         case each replaced file keeps one `.bak` -- then each .py through the ordinary
         `_write_tool` (which also keeps a `.bak`), and
      5. puts everything back as it was if any of that fails: nothing is half-imported.
    A screen file of an older version that the new bundle doesn't have is left alone.
    """
    import tempfile
    from . import tool_ui
    plan = plan_import(items, name, names=names, overwrite=overwrite)
    files = plan.pop("_files", {})
    if not plan.get("ok"):
        return plan
    if not write:
        return plan
    blockers = [t["name_problem"] for t in plan["tools"] if t["name_problem"]] + list(plan["problems"])
    if blockers:
        return dict(plan, ok=False, stage="import", error="; ".join(blockers))
    entries = [(t["py_file"], t["name"], files[t["py_file"]]) for t in plan["tools"]]
    web = {rel: files[rel] for rel in files if rel not in {e[0] for e in entries}}

    checks = {}
    with tempfile.TemporaryDirectory(prefix="jarvis_import_") as stage_dir:
        stage = Path(stage_dir)
        for rel, text in web.items():
            target = stage.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding=ENCODING)
        used = set()
        for py_file, final, source in entries:
            check = validate_source(source, final, base_dir=stage)
            if not check["ok"]:
                return {"ok": False, "stage": check.get("stage", "validate"), "error": check["error"],
                        "hint": check.get("hint", ""), "name": final, "py_file": py_file,
                        "web": plan["web"], "skipped": plan["skipped"]}
            checks[final] = check
            for entry in check.get("ui", []):
                for item in tool_ui.element_files(entry, stage):
                    if item["exists"]:
                        used.add(item["rel"])
    clash = _bundle_clashes(entries, checks)
    if clash:
        return {"ok": False, "stage": "import", "error": clash, "web": plan["web"], "skipped": plan["skipped"]}
    skipped = list(plan["skipped"])
    for rel in sorted(set(web) - used):
        skipped.append({"path": rel, "why": "no screen this tool declares uses it"})
    to_write = {rel: web[rel] for rel in sorted(web) if rel in used}

    created_files, created_dirs, replaced_files = [], [], []     # replaced: (target, bytes, bak-bytes-or-None)
    written_tools = []                                           # (name, snapshot)

    def roll_back():
        for tool_name, snap in reversed(written_tools):
            _undo_tool(tool_name, snap)
        for target, original, bak in reversed(replaced_files):
            bak_path = target.with_name(target.name + ".bak")
            try:
                target.write_bytes(original)
                if bak is None:
                    bak_path.unlink()
                else:
                    bak_path.write_bytes(bak)
            except OSError:
                pass
        for path in reversed(created_files):
            try:
                path.unlink()
            except OSError:
                pass
        for path in reversed(created_dirs):
            try:
                path.rmdir()
            except OSError:
                pass

    ensure_dir()
    rel = ""
    try:
        for rel, text in to_write.items():
            target = TOOLS_DIR.joinpath(*rel.split("/"))
            if target.is_file() and not target.is_symlink():
                if not plan["overwrite"]:
                    raise OSError("%s appeared while importing and was not replaced" % rel)
                bak_path = target.with_name(target.name + ".bak")
                original = target.read_bytes()
                bak = bak_path.read_bytes() if bak_path.is_file() else None
                shutil.copyfile(target, bak_path)
                replaced_files.append((target, original, bak))
                _atomic_text(target, text)
                continue
            missing, cur = [], target.parent
            while not cur.exists() and cur != TOOLS_DIR:
                missing.append(cur)
                cur = cur.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing))
            with open(str(target), "x", encoding=ENCODING) as handle:     # "x": never over a file that isn't there to replace
                handle.write(text)
            created_files.append(target)
    except OSError as exc:
        roll_back()
        return {"ok": False, "stage": "import", "error": "couldn't write %s: %s" % (rel, exc),
                "name": entries[0][1], "rolled_back": True}
    results = []
    for py_file, final, source in entries:
        snap = _snapshot_tool(final)
        result = _write_tool(final, source, description)
        if not result.get("ok"):
            roll_back()
            return dict(result, name=final, py_file=py_file, rolled_back=True)
        written_tools.append((final, snap))
        results.append(result)
    out = dict(results[0])
    out["name"] = results[0]["name"]
    out["names"] = [r["name"] for r in results]
    if len(results) > 1:
        out["tools"] = [t for r in results for t in (r.get("tools") or [])]
        out["results"] = results
    out["imported"] = ["%s.py" % r["name"] for r in results] + list(to_write)
    out["skipped"] = skipped
    out["replaced"] = {"tools": [n for n, snap in written_tools if snap is not None],
                       "files": [t.relative_to(TOOLS_DIR).as_posix() for t, _o, _b in replaced_files]}
    return out


def _bundle_clashes(entries, checks):
    """A sentence when two tools of one bundle declare the same tool function, screen id or
    palette command -- the loader keeps the first and drops the second, and the single-file
    checks can't see it because each ran alone. '' when there is no clash."""
    kinds = (("tool", lambda c: list(c.get("tools") or [])),
             ("screen id", lambda c: [u.get("id") for u in c.get("ui") or []]),
             ("palette command", lambda c: [x.get("name") for x in c.get("commands") or []]))
    for label, pick in kinds:
        owner = {}
        for _py, final, _src in entries:
            for key in pick(checks[final]):
                if not key:
                    continue
                if key in owner and owner[key] != final:
                    return "%s and %s both declare the %s %r; the loader would keep one and drop the other" % (
                        owner[key], final, label, key)
                owner.setdefault(key, final)
    return ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_source(source, name="custom_tool", base_dir=None):
    """Compile and inspect without registering anything.

    Three stages, each reporting separately, because "syntax error on line
    12" and "your TOOLS dict is missing a handler" need completely different
    fixes and a single "invalid" would make the user guess which they hit.

    Executing the module is unavoidable — TOOL_SCHEMAS is a module-level
    value, so there is no way to read it without running the top level. That
    is the same exposure tool_loader already accepts for actions/, and the
    person whose file it is is the person who asked to save it.

    `base_dir` is where a TOOL_UI `path` is resolved from (default: ~/.jarvis/tools,
    where the file will live). An import checks a bundle against a staging folder
    that already holds its screen files, so "the folder isn't there yet" doesn't
    hide what the tool declares.
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

    # The loader (tool_loader._validate) rejects the whole file for these three
    # as well. Without the same checks here the editor said "valid", Save wrote
    # the file, and the tool then silently never loaded -- the exact failure
    # this editor exists to prevent. Same wording as the loader on purpose.
    keywords = getattr(module, "TOOL_KEYWORDS", {}) or {}
    if not isinstance(keywords, dict):
        return {"ok": False, "stage": "contract",
                "error": "TOOL_KEYWORDS must be a dict if present."}
    stray_kw = set(keywords) - set(names)
    if stray_kw:
        return {"ok": False, "stage": "contract",
                "error": "TOOL_KEYWORDS has entries for name(s) not in this file's TOOLS: %s."
                         % sorted(stray_kw),
                "hint": "Rename the TOOL_KEYWORDS keys to match your tool names, "
                        "or remove them."}
    if not isinstance(getattr(module, "TOOL_RESULT_SPECS", {}) or {}, dict):
        return {"ok": False, "stage": "contract",
                "error": "TOOL_RESULT_SPECS must be a dict if present."}

    # Collide with a built-in and the loader will reject the whole file at
    # startup — better to say so now, while the editor is still open.
    clash = _builtin_clash(names)
    if clash:
        return {"ok": False, "stage": "contract",
                "error": "%s already exists as a built-in tool — pick another name"
                         % ", ".join(clash)}

    # G.1/G.2: the file's own Test Checklist entries (and, if it uses the
    # multi-group TEST_CHECKLIST_GROUP shape, its own group ids). Never a
    # reason to fail the check (a malformed entry is dropped at discovery,
    # the tool still loads — see tool_loader.py), but the ONLY place a
    # custom-tool author gets told: discovery's log line goes to stderr,
    # where nobody is looking. _group_meta itself isn't needed here — the
    # panel only ever reads group labels from `jarvis tools-list` (see
    # tools.py's tools_list_payload()) — but a bad multi-group id on an
    # entry (one that matches neither TOOL_GROUP nor a declared group) still
    # surfaces the same way any other malformed entry does, via
    # checklist_problems below.
    from . import checklist_schema
    entries, _group_meta, checklist_problems = checklist_schema.extract_supplied(
        getattr(module, "TEST_CHECKLIST", None),
        getattr(module, "TEST_CHECKLIST_GROUP", None),
        set(names), group.strip(),
    )

    # TOOL_UI (tool_ui.py): like the checklist, never a reason to fail the check --
    # the loader drops a bad entry and the tools still load -- but the only place
    # the author hears about it. Paths resolve against ~/.jarvis/tools, where this
    # file will live, so "the folder isn't there yet" is reported honestly.
    ui_entries, ui_problems = [], []
    if getattr(module, "TOOL_UI", None) is not None:
        from . import tool_ui
        ui_entries, ui_problems = tool_ui.validate_ui(
            getattr(module, "TOOL_UI", None), "%s.py" % name, base_dir or TOOLS_DIR, set(names))
        ui_problems = list(ui_problems) + _ui_clashes(ui_entries, name)

    # TOOL_COMMANDS (tool_commands.py, I.2.11): same rule again -- the loader drops a
    # bad entry and the tools still load, and this is the only place the author hears
    # about it (including "you declared no riskTier, so it asks first").
    command_entries, command_problems = [], []
    if getattr(module, "TOOL_COMMANDS", None) is not None:
        from . import tool_commands
        by_tool = {sch.get("name"): sch for sch in schemas if isinstance(sch, dict)}
        command_entries, command_problems = tool_commands.validate_commands(
            getattr(module, "TOOL_COMMANDS", None), "%s.py" % name, set(names), by_tool)
        command_problems = list(command_problems) + _command_clashes(command_entries, name)

    return {"ok": True, "tools": names, "group": group.strip(),
            "keywords": getattr(module, "TOOL_KEYWORDS", {}) or {},
            "checklist": sorted(entries),
            "checklist_missing": [n for n in names if n not in entries],
            "checklist_problems": checklist_problems,
            "ui": [tool_ui_public(u) for u in ui_entries],
            "ui_problems": ui_problems,
            "commands": [tool_commands.public(c) for c in command_entries] if command_entries else [],
            "command_problems": command_problems}


def tool_ui_public(entry):
    """A TOOL_UI entry as the editor shows it (no absolute paths)."""
    from . import tool_ui
    return tool_ui.public(entry)


def _ui_clashes(entries, name):
    """UI element ids already taken by ANOTHER file. Same rule as tool names: the
    first registration wins and shipped files are scanned before yours, so a clash
    means this element will be dropped at the next start -- say so now."""
    try:
        from . import tools as system_tools
        taken = {u["id"]: u.get("file", "") for u in getattr(system_tools, "AUTO_UI", []) or []}
    except Exception:  # noqa: BLE001 -- during partial init, skip the check
        return []
    mine = "%s.py" % name
    return ["UI element id %r is already used by %s -- this one will be ignored" % (u["id"], taken[u["id"]])
            for u in entries if taken.get(u["id"]) not in (None, "", mine)]


def _command_clashes(entries, name):
    """Palette command names already taken by ANOTHER file (I.2.11). Same rule as tool
    names and UI ids: first registration wins and shipped files are scanned before
    yours, so a clash means this command will be dropped at the next start."""
    try:
        from . import tools as system_tools
        taken = {c["name"]: c.get("file", "") for c in getattr(system_tools, "AUTO_COMMANDS", []) or []}
    except Exception:  # noqa: BLE001 -- during partial init, skip the check
        return []
    mine = "%s.py" % name
    return ["palette command %r is already used by %s -- this one will be ignored" % (c["name"], taken[c["name"]])
            for c in entries if taken.get(c["name"]) not in (None, "", mine)]


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


# The two templates that ship a TOOL_UI page (and its starter folder). A separate
# module only because the html/js/css inside them is unpleasant to read in a string
# shared with a dozen other templates; to every caller they are ordinary templates.
from .custom_tools_ui_templates import UI_TEMPLATES  # noqa: E402

TEMPLATES.update(UI_TEMPLATES)


def templates():
    return [{"id": k, "label": v["label"], "hint": v["hint"],
             "folder": v.get("folder", ""), "files": sorted(v.get("files") or {})}
            for k, v in TEMPLATES.items()]


def template_source(template_id):
    entry = TEMPLATES.get(template_id) or TEMPLATES["minimal"]
    return entry["source"]


def template_files(template_id):
    """{"folder": ..., "files": [names]} for a template that ships a starter
    folder (the two TOOL_UI ones), else empty -- what the editor tells you saving
    will also create."""
    entry = TEMPLATES.get(template_id) or {}
    return {"folder": entry.get("folder", ""), "files": sorted(entry.get("files") or {})}


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
    TOOL_UI               = [{"id", "label", "mode", "path"}]  # a button or Menu entry that
                                              # opens a page/panel built from your own
                                              # tool.html / tool.js / tool.css in a folder

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
* `_drafts/` holds backups of tools you were still writing (saved or not). They are
  copies, never loaded as tools; restore or delete them from the Tool Manager.
* A `TOOL_UI` folder sits beside your .py (e.g. `~/.jarvis/tools/my_page/`). Its
  `path` is relative to the .py that declares it and can't point outside this folder.
* `x.py.disabled` is switched off but kept.

To show something on screen or ask a question:

    from jarvis import ui_bridge as ui
    ui.toast("done", level="success")
    if ui.confirm("Really?", default=False): ...

Manage these from the web UI: Menu > Tool Manager.
"""
