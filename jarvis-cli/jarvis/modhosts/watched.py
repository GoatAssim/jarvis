"""The watched mod folders (brief M1): the folders the owner put on the Minecraft update list.

The Updates panel can only update mods in a folder the owner chose on purpose.  This module is that list, and
nothing else.  It does not read a folder, hash a file, ask a mod site or replace anything: the check is
`modhosts/updates.py`, the replace engine is `modhosts/apply.py`, and the tools that call them are in
`actions/minecraft_update_source.py`.  NO NETWORK and no file of the owner's is touched here: the only file this
module writes is its own state file.

THE STATE FILE  ~/.jarvis/modhosts/watched.json   {"v": 1, "folders": [entry, ...]}
  entry = {"folder": str, "label": str | None, "mc_version": str | None, "loader": str | None,
           "channel": "release" | "beta" | "alpha", "auto": bool}
  Same habits as update_state.json (modhosts/updates.py):
    * written atomically (a temp file in the same folder, fsync, then os.replace);
    * size-capped (MAX_BYTES) and at most MAX_FOLDERS folders;
    * a missing file is an empty list; a DAMAGED file (not JSON, the wrong shape, too big) reads as an empty list
      and is NOT rewritten until the owner changes something (add / remove / set); `load()` says `damaged: True`
      so a caller can tell "nothing watched" from "the list could not be read";
    * one bad entry inside a good file is dropped on read (counted in `dropped`) and is gone after the next change;
    * it holds no key, no token and no vendor text: only what the owner typed, clipped and stripped of markup.

THE RULES (tests/test_minecraft_update_source.py pins each one)
  * `folder` is an absolute path with no `..` and no drive root.  It must pass `outdir.protected_reason` as written
    AND as it really resolves (so ~/.jarvis, the home folder itself, OS folders and a link that points into them
    are refused), must not itself be a link, and must exist as a folder WHEN IT IS ADDED.  A folder that is gone
    later stays on the list (the panel says so): `folders()` reports `exists` and `problem`.
  * Two entries never name the same folder (compared the way the check compares paths: `outdir.norm`).
  * `mc_version` and `loader` are the owner's own answer.  When they are not given the check says
    needs_mc_version / needs_loader for the files that need it, and that is passed on as a problem text: this module
    never guesses either one.
  * `auto` is the owner's per-folder wish and defaults to FALSE.  The hub's own switch is separate and wins; nothing
    here acts on it.
  * Every function returns a plain dict and never raises.  A failure is {"ok": False, "error", "reason"} (the
    reasons of modhosts/__init__.py) with one plain sentence.

A FOLDER ID.  `folder_id(folder)` is "f" + the first 8 hex digits of the SHA-1 of the normalised path.  It does not
depend on the order of the list (removing the first folder does not renumber the rest), so a finding id built from it
(`<folder id>:<sha1 of the file>`, see actions/minecraft_update_source.py) stays valid until its folder leaves the list.
"""
import hashlib
import json
import os
import re
import threading
from pathlib import Path

from . import BAD_DEST, EXISTS, NOT_FOUND, TOO_BIG, UNSUPPORTED, fail, outdir, untrusted

MAX_FOLDERS = 20
MAX_BYTES = 64 * 1024
LABEL_LEN = 60
CHANNELS = ("release", "beta", "alpha")
FIELDS = ("label", "mc_version", "loader", "channel", "auto")
_ID_RE = re.compile(r"^f[0-9a-f]{8}$")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

# One writer at a time inside this process (the hub's thread and a chat tool can both reach here).  Two PROCESSES
# (the CLI and the web server) are not locked against each other: the last write wins, and each write re-reads the file
# first so it only loses a change made in the same few milliseconds.
_LOCK = threading.RLock()


# --------------------------------------------------------------------------- the file

def state_path():
    """Resolved at call time so a test that redirects HOME first sees its own folder."""
    return Path.home() / ".jarvis" / "modhosts" / "watched.json"


def folder_id(folder):
    """The stable short id of a folder (see the module docstring)."""
    text = outdir.norm(os.path.expanduser(str(folder)))
    return "f" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]


def is_folder_id(text):
    return isinstance(text, str) and bool(_ID_RE.match(text))


def _mc(text):
    from . import resolve
    return resolve.normalise_mc_version(text)


def _loader(text):
    from . import resolve
    return resolve.normalise_loader(text)


def _bool(value):
    """True / False for a bool or the usual yes / no words; None when it is neither."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    return None


def _entry_from_file(raw):
    """One entry as read from the file -> a clean entry, or None when it cannot be used.  A field that is not valid
    is dropped to its default (so a hand-edited mc_version that is not a version means 'ask me', never a guess)."""
    if not isinstance(raw, dict):
        return None
    folder = raw.get("folder")
    if not isinstance(folder, str) or not folder.strip() or "\x00" in folder:
        return None
    try:
        path = Path(os.path.expanduser(folder.strip()))
    except Exception:  # noqa: BLE001
        return None
    if not path.is_absolute() or ".." in path.parts or len(path.parts) <= 1:
        return None
    label = raw.get("label")
    label = untrusted.clean_name(label, LABEL_LEN) if isinstance(label, str) else ""
    mc = raw.get("mc_version")
    mc = _mc(mc) if isinstance(mc, str) and mc.strip() else None
    loader = raw.get("loader")
    loader = _loader(loader) if isinstance(loader, str) and loader.strip() else None
    channel = raw.get("channel")
    channel = channel.strip().lower() if isinstance(channel, str) else "release"
    return {"folder": str(path), "label": label or None, "mc_version": mc, "loader": loader,
            "channel": channel if channel in CHANNELS else "release", "auto": raw.get("auto") is True}


def load():
    """Read the list.  -> {"ok": True, "entries": [entry], "damaged": bool, "dropped": int, "path": str}.  Never raises
    and NEVER writes: a damaged file stays exactly as it is until the owner changes something."""
    out = {"ok": True, "entries": [], "damaged": False, "dropped": 0, "path": str(state_path())}
    try:
        with state_path().open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return out                                            # nothing watched yet
    except Exception:  # noqa: BLE001 - locked, no permission, a folder in the way
        out["damaged"] = True
        return out
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("too big")
        data = json.loads(raw.decode("utf-8"))
        items = data.get("folders") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError("wrong shape")
    except Exception:  # noqa: BLE001 - not JSON, not UTF-8, nested too deep, the wrong shape: read as empty
        out["damaged"] = True
        return out
    seen = set()
    for item in items:
        entry = _entry_from_file(item)
        key = outdir.norm(entry["folder"]) if entry else None
        if entry is None or key in seen or len(out["entries"]) >= MAX_FOLDERS:
            out["dropped"] += 1
            continue
        seen.add(key)
        out["entries"].append(entry)
    return out


def _save(entries):
    """Write the whole list atomically -> None when written, else the failure.  Never raises."""
    tmp = None
    try:
        text = json.dumps({"v": 1, "folders": [_for_file(e) for e in entries]}, separators=(",", ":"),
                          ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_BYTES:
            return fail(TOO_BIG, "The watched list would be too big to save; shorten the labels or remove a folder.")
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(str(tmp), str(path))
        tmp = None
        return None
    except Exception as exc:  # noqa: BLE001 - this never raises
        return fail(UNSUPPORTED, "The watched list could not be saved (%s). Nothing was changed." % type(exc).__name__)
    finally:
        if tmp is not None:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass


def _for_file(entry):
    return {"folder": entry["folder"], "label": entry.get("label"), "mc_version": entry.get("mc_version"),
            "loader": entry.get("loader"), "channel": entry.get("channel") or "release", "auto": entry.get("auto") is True}


def _public(entry):
    out = _for_file(entry)
    out["id"] = folder_id(entry["folder"])
    return out


# --------------------------------------------------------------------------- the folder rule

def _vet_folder(raw):
    """A folder the owner names -> (path text, None) | (None, failure).  The same rule as the update check's
    `resolve_folder`, built on the same primitives (outdir.protected_reason / outdir.is_link)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, fail(UNSUPPORTED, "Give the full path of the mods folder, for example D:/Minecraft/mods.")
    text = raw.strip()
    if "\x00" in text:
        return None, fail(BAD_DEST, "That folder name has an invalid character.")
    try:
        path = Path(os.path.expanduser(text))
    except Exception:  # noqa: BLE001
        return None, fail(BAD_DEST, "That folder name can't be used.")
    if not path.is_absolute():
        return None, fail(BAD_DEST, "Give a full folder path like D:/Minecraft/mods; '%s' isn't one." % text[:80])
    if ".." in path.parts or len(path.parts) <= 1:
        return None, fail(BAD_DEST, "'%s' isn't usable: no '..' and no drive root." % text[:80])
    why = outdir.protected_reason(path, writing=True)
    if why:
        return None, fail(BAD_DEST, "Won't watch that folder: %s." % why)
    if outdir.is_link(path):
        return None, fail(BAD_DEST, "Won't watch a symlink/shortcut folder (%s); name the real folder." % str(path)[:120])
    if not os.path.isdir(path):
        return None, fail(BAD_DEST, "%s is not a folder that exists." % str(path)[:120])
    return str(path), None


def problem_of(folder):
    """Why a listed folder cannot be checked right now, or None."""
    try:
        path = Path(folder)
        why = outdir.protected_reason(path, writing=True)
        if why:
            return "not allowed: %s" % why
        if outdir.is_link(path):
            return "it is a symlink or shortcut"
        if not os.path.isdir(path):
            return "the folder was not found"
    except Exception:  # noqa: BLE001
        return "the folder could not be read"
    return None


# --------------------------------------------------------------------------- fields

def _clean_fields(given):
    """The fields the owner supplied (a dict of only the ones given) -> (clean dict, None) | (None, failure).
    None or "" for label / mc_version / loader clears it; auto and channel must be valid."""
    out = {}
    for key, value in given.items():
        if key not in FIELDS:
            return None, fail(UNSUPPORTED, "'%s' is not something a watched folder has (use label, mc_version, "
                                           "loader, channel or auto)." % str(key)[:30])
        if key == "label":
            if value is not None and not isinstance(value, str):
                return None, fail(UNSUPPORTED, "label must be text.")
            out[key] = (untrusted.clean_name(value, LABEL_LEN) or None) if value else None
        elif key == "mc_version":
            if value is None or (isinstance(value, str) and not value.strip()):
                out[key] = None
            elif not isinstance(value, str) or _mc(value) is None:
                return None, fail(UNSUPPORTED, "%r is not a Minecraft version like 1.21.1 or 26.3 (give it as text)."
                                  % str(value)[:30])
            else:
                out[key] = _mc(value)
        elif key == "loader":
            if value is None or (isinstance(value, str) and not value.strip()):
                out[key] = None
            elif not isinstance(value, str) or _loader(value) is None:
                return None, fail(UNSUPPORTED, "%r is not a loader Jarvis knows: use fabric, forge, neoforge or quilt."
                                  % str(value)[:30])
            else:
                out[key] = _loader(value)
        elif key == "channel":
            text = value.strip().lower() if isinstance(value, str) else ""
            if text not in CHANNELS:
                return None, fail(UNSUPPORTED, "channel must be release, beta or alpha.")
            out[key] = text
        else:
            flag = _bool(value)
            if flag is None:
                return None, fail(UNSUPPORTED, "auto must be true or false.")
            out[key] = flag
    return out, None


def find(entries, text):
    """The entry named by a folder path or a folder id -> entry | None."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    if is_folder_id(text):
        for entry in entries:
            if folder_id(entry["folder"]) == text:
                return entry
    try:
        key = outdir.norm(os.path.expanduser(text))
    except Exception:  # noqa: BLE001
        return None
    for entry in entries:
        if outdir.norm(entry["folder"]) == key:
            return entry
    return None


def describe(entry):
    """A short name for an entry in a sentence: its label, else the last part of its folder."""
    label = entry.get("label")
    if label:
        return label
    tail = os.path.basename(str(entry.get("folder") or "").rstrip("\\/"))
    return tail or str(entry.get("folder") or "")[:40]


# --------------------------------------------------------------------------- the functions

def folders(loaded=None):
    """The watched folders for a screen or a check: each entry plus `id`, `exists` (the folder is still there) and
    `problem` (why it cannot be checked now, or None).  Pass the dict `load()` returned to avoid reading twice."""
    try:
        loaded = loaded if isinstance(loaded, dict) else load()
        out = []
        for entry in loaded.get("entries") or []:
            item = _public(entry)
            item["exists"] = os.path.isdir(entry["folder"])
            item["problem"] = problem_of(entry["folder"])
            out.append(item)
        return out
    except Exception:  # noqa: BLE001 - never raises
        return []


def add(folder, label=None, mc_version=None, loader=None, channel=None, auto=None, dry_run=False):
    """Put a folder on the list.  -> {"ok": True, "dry_run", "entry", "count", "message"} | a failure.  With `dry_run`
    nothing is written."""
    try:
        with _LOCK:
            text, bad = _vet_folder(folder)
            if bad:
                return bad
            given = {k: v for k, v in (("label", label), ("mc_version", mc_version), ("loader", loader),
                                       ("channel", channel), ("auto", auto)) if v is not None}
            clean, bad = _clean_fields(given)
            if bad:
                return bad
            loaded = load()
            entries = list(loaded["entries"])
            if find(entries, text) is not None:
                return fail(EXISTS, "That folder is already on the watched list; use set to change it.")
            if len(entries) >= MAX_FOLDERS:
                return fail(TOO_BIG, "At most %d folders can be watched; remove one first." % MAX_FOLDERS)
            entry = {"folder": text, "label": None, "mc_version": None, "loader": None, "channel": "release",
                     "auto": False}
            entry.update(clean)
            if not dry_run:
                bad = _save(entries + [entry])
                if bad:
                    return bad
            return {"ok": True, "dry_run": bool(dry_run), "entry": _public(entry), "count": len(entries) + 1,
                    "message": ("Would put %s on the watched list. Nothing was written." if dry_run
                                else "%s is on the watched list now.") % describe(entry)}
    except Exception as exc:  # noqa: BLE001 - never raises
        return fail(UNSUPPORTED, "The watched list could not be changed (%s). Nothing was changed." % type(exc).__name__)


def remove(folder, dry_run=False):
    """Take a folder (a path or a folder id) off the list.  Only the list changes: the folder and its files are not
    touched."""
    try:
        with _LOCK:
            loaded = load()
            entries = list(loaded["entries"])
            found = find(entries, folder)
            if found is None:
                return fail(NOT_FOUND, "That folder is not on the watched list.")
            if not dry_run:
                bad = _save([e for e in entries if e is not found])
                if bad:
                    return bad
            return {"ok": True, "dry_run": bool(dry_run), "entry": _public(found), "count": len(entries) - 1,
                    "message": ("Would take %s off the watched list; the folder and its files are not touched. "
                                "Nothing was written." if dry_run else
                                "%s is off the watched list; the folder and its files were not touched.")
                    % describe(found)}
    except Exception as exc:  # noqa: BLE001
        return fail(UNSUPPORTED, "The watched list could not be changed (%s). Nothing was changed." % type(exc).__name__)


def update_entry(folder, dry_run=False, **fields):
    """Change fields of a listed folder (label, mc_version, loader, channel, auto).  A field that is not given stays;
    None or "" clears label / mc_version / loader.  -> {"ok": True, "changed": bool, ...} | a failure."""
    try:
        with _LOCK:
            if not fields:
                return fail(UNSUPPORTED, "Say what to change: label, mc_version, loader, channel or auto.")
            clean, bad = _clean_fields(fields)
            if bad:
                return bad
            loaded = load()
            entries = list(loaded["entries"])
            found = find(entries, folder)
            if found is None:
                return fail(NOT_FOUND, "That folder is not on the watched list.")
            changed = {k: v for k, v in clean.items() if found.get(k) != v}
            new = dict(found)
            new.update(changed)
            if changed and not dry_run:
                bad = _save([new if e is found else e for e in entries])
                if bad:
                    return bad
            if not changed:
                message = "%s already has those settings. Nothing was changed." % describe(found)
            elif dry_run:
                message = "Would change %s for %s. Nothing was written." % (", ".join(sorted(changed)), describe(found))
            else:
                message = "Changed %s for %s." % (", ".join(sorted(changed)), describe(found))
            return {"ok": True, "dry_run": bool(dry_run), "changed": bool(changed), "entry": _public(new),
                    "count": len(entries), "message": message}
    except Exception as exc:  # noqa: BLE001
        return fail(UNSUPPORTED, "The watched list could not be changed (%s). Nothing was changed." % type(exc).__name__)
