"""Update checks for the Minecraft files in ONE folder the owner names (brief S2).  Nothing is ever changed.

The owner asks "do my mods have newer files?"; this answers with a list.  It never downloads, replaces, renames or
installs anything: it reads the folder, asks Modrinth, and reports.  (To fetch a newer file the owner asks for it
with `download_mc_file`, which queues it in Downloads and never touches the old jar.)

HOW A FILE IS REMEMBERED (the open design question)
  It is not.  The folder IS the record: every .jar / .zip / .mrpack in it is hashed (SHA-1) and Modrinth is asked who
  those hashes belong to (`POST /version_files`).  A file is matched to a project by its CONTENT, never by its name, so a
  renamed jar still matches and a look-alike name never does.  The only thing kept on disk is a small state file,
  ~/.jarvis/modhosts/update_state.json: a hash cache (path, size, modified time -> SHA-1, so an unchanged jar is not
  read again) and the last folder checked.  It holds no key, no vendor answer and nothing that cannot be rebuilt; losing
  it costs one re-hash.  `hub/` is not touched and no other module's rows are read.

SAFETY RULES (tests/test_minecraft_updates.py pins each one)
  * The folder is an absolute path with no `..` and no drive root.  It must pass `outdir.protected_reason` as written AND
    as it really resolves (so ~/.jarvis, the home folder itself, OS folders and a link that points into them are refused).
    It must exist and must not itself be a link (symlink / Windows junction).
  * The scan is ONE level deep: a sub-folder is counted and never entered.  Links, hidden files (a leading dot, or the
    Windows hidden attribute), non-regular files and every extension but .jar / .zip / .mrpack are counted and never
    opened.  A file is opened with O_NOFOLLOW where the system has it and re-checked as a regular file after opening.
  * Only hashes leave the PC (to api.modrinth.com, through net.py: host allow-list, https, User-Agent).  No file name, no
    path and no content is sent.
  * Vendor text (project and version names, file names) is clipped and marked untrusted.
  * A file Modrinth does not know is "not checked", never guessed.  A file that came from CurseForge is in that group:
    CurseForge identifies files by its own fingerprint (murmur2 of the file), which this check does not compute, and a
    name or a size is not evidence.  It is reported as not checked with that reason.
  * "Newer" means a file Modrinth published AFTER the installed one for the same Minecraft version and loader, on a
    channel at least as stable as the installed file (or the `channel` asked for).  A version number is never compared.
  * Minecraft version and loader come from the owner, else from the installed file's own listing when it names exactly
    one.  A file that lists several (or none) and was given no answer is "not checked: needs a version / loader", never
    a guess.  A file built for another version / loader than the one asked about is reported as such, not as "up to date".

The module's functions return plain dicts and never raise.  A failure is {"ok": False, "error", "reason"} with a reason
from modhosts/__init__.py; a per-file "not checked" is a status inside an ok result (`why` is its own short code).
"""
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from . import BAD_DEST, OFFLINE, UNSUPPORTED, VENDOR_CHANGED, fail, net, outdir, sources, untrusted

BASE = "https://api.modrinth.com/v2"
EXTENSIONS = (".jar", ".zip", ".mrpack")
CHANNELS = ("release", "beta", "alpha")
_RANK = {"release": 0, "beta": 1, "alpha": 2}

HASH_CHUNK = 1024 * 1024
HASH_BATCH = 100                 # hashes per request (a vendor batch size, not one of the owner's limits)
PROJECT_BATCH = 100
MAX_SCAN_ENTRIES = 50000         # defensive: a folder this big is not a mods folder; protects memory only
STATE_MAX_HASHES = 20000
STATE_MAX_BYTES = 4 * 1024 * 1024
MAX_LISTED_VERSIONS = 12         # versions named in a "needs a Minecraft version" sentence
_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?")

CURSEFORGE_NOTE = ("A file Modrinth does not know may have come from CurseForge. CurseForge identifies its files by its "
                   "own fingerprint, which this check does not compute, so such files are reported as not checked. "
                   "Nothing was guessed from a file name or size.")


# --------------------------------------------------------------------------- small helpers

def _late(deadline):
    return deadline is not None and time.monotonic() > deadline


def _when(text):
    """An ISO timestamp as a sortable tuple, or None when it is not one."""
    match = _TS_RE.match(str(text or ""))
    if not match:
        return None
    try:
        parts = [int(x) for x in match.groups()[:6]]
    except ValueError:
        return None
    return tuple(parts) + (int((match.group(7) or "0").ljust(9, "0")),)


def _rank(channel, default=2):
    return _RANK.get(str(channel or "").lower(), default)


def _strings(value):
    return [str(x) for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _day(text):
    return str(text or "")[:10]


# --------------------------------------------------------------------------- the state file

def state_path():
    """Resolved at call time so a test that redirects HOME first sees its own folder."""
    return Path.home() / ".jarvis" / "modhosts" / "update_state.json"


def load_state():
    """{"last_folder": str | None, "hashes": {key: [size, mtime_ns, sha1]}}; anything unreadable is an empty state."""
    empty = {"last_folder": None, "hashes": {}}
    try:
        with state_path().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001 - missing, torn or unreadable: nothing remembered
        return empty
    try:
        if not isinstance(data, dict):
            return empty
        hashes = {}
        raw = data.get("hashes")
        for key, value in (raw.items() if isinstance(raw, dict) else ()):
            if (isinstance(key, str) and isinstance(value, list) and len(value) == 3
                    and isinstance(value[0], int) and isinstance(value[1], int)
                    and isinstance(value[2], str) and _SHA1_RE.match(value[2])):
                hashes[key] = value
        last = data.get("last_folder")
        return {"last_folder": last if isinstance(last, str) and last else None, "hashes": hashes}
    except Exception:  # noqa: BLE001
        return empty


def save_state(state):
    """Write the state file atomically (temp file, then replace).  True when written; never raises."""
    try:
        hashes = dict(state.get("hashes") or {})
        while len(hashes) > STATE_MAX_HASHES:
            hashes.pop(next(iter(hashes)))                    # the oldest-inserted entry goes first
        text = json.dumps({"v": 1, "last_folder": state.get("last_folder"), "hashes": hashes},
                          separators=(",", ":"), ensure_ascii=False)
        while len(text.encode("utf-8")) > STATE_MAX_BYTES and hashes:
            for _ in range(max(1, len(hashes) // 10)):
                hashes.pop(next(iter(hashes)))
            text = json.dumps({"v": 1, "last_folder": state.get("last_folder"), "hashes": hashes},
                              separators=(",", ":"), ensure_ascii=False)
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
        return True
    except Exception:  # noqa: BLE001 - a state file that fails is only a missed cache
        return False


# --------------------------------------------------------------------------- the folder

def resolve_folder(raw, default="~/Downloads", state=None):
    """The folder to read -> (Path, where_it_came_from, None) | (None, None, failure).

    `raw` (what the owner named) wins; else the last folder checked; else `default`.  Nothing is created."""
    text, how = "", "default"
    if isinstance(raw, str) and raw.strip():
        text, how = raw.strip(), "given"
    elif isinstance(state, dict) and state.get("last_folder"):
        text, how = str(state["last_folder"]), "last_checked"
    else:
        text = str(default or "~/Downloads")
    if "\x00" in text:
        return None, None, fail(BAD_DEST, "That folder name has an invalid character.")
    try:
        path = Path(os.path.expanduser(text))
    except Exception:  # noqa: BLE001
        return None, None, fail(BAD_DEST, "That folder name can't be used.")
    if not path.is_absolute():
        return None, None, fail(BAD_DEST, "Give a full folder path like D:/Minecraft/mods; '%s' isn't one." % text[:80])
    if ".." in path.parts or len(path.parts) <= 1:
        return None, None, fail(BAD_DEST, "'%s' isn't usable: no '..' and no drive root." % text[:80])
    why = outdir.protected_reason(path, writing=True)
    if why:
        return None, None, fail(BAD_DEST, "Won't look there: %s." % why)
    if outdir.is_link(path):
        return None, None, fail(BAD_DEST, "Won't read a symlink/shortcut folder (%s); name the real folder." % str(path)[:120])
    if not os.path.isdir(path):
        return None, None, fail(BAD_DEST, "%s is not a folder that exists." % str(path)[:120])
    return path, how, None


def _hash_file(path):
    """SHA-1 of one regular file -> (hex, None) | (None, why).  Never follows a link; never raises."""
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "not_a_file"
        digest = hashlib.sha1()
        with os.fdopen(fd, "rb") as handle:
            fd = None                                         # the file object owns it now
            while True:
                chunk = handle.read(HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest(), None
    except Exception:  # noqa: BLE001 - locked, vanished, no permission
        return None, "unreadable"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def scan_folder(folder, cached_hashes=None, deadline=None):
    """Hash the mod-like files of ONE folder (not its sub-folders).

    -> {"ok": True, "files": [{"name", "key", "size", "mtime_ns", "sha1"}], "unreadable": [name], "ignored": {...},
        "partial": bool, "hashes": {key: [size, mtime_ns, sha1]}}  |  a failure."""
    cached = cached_hashes if isinstance(cached_hashes, dict) else {}
    ignored = {"other_files": 0, "subfolders": 0, "hidden": 0, "links": 0}
    files, unreadable, fresh = [], [], {}
    partial = False
    try:
        with os.scandir(str(folder)) as it:
            entries = []
            for entry in it:
                entries.append(entry)
                if len(entries) > MAX_SCAN_ENTRIES:
                    partial = True
                    break
        entries.sort(key=lambda e: (e.name.lower(), e.name))
    except OSError as exc:
        return fail(BAD_DEST, "Couldn't read %s (%s)." % (str(folder)[:120], str(exc)[:80]))
    for entry in entries:
        name = entry.name
        path = os.path.join(str(folder), name)
        try:
            if name.startswith("."):
                ignored["hidden"] += 1
                continue
            if outdir.is_link(path):
                ignored["links"] += 1
                continue
            st = entry.stat(follow_symlinks=False)
            if getattr(st, "st_file_attributes", 0) & _HIDDEN:
                ignored["hidden"] += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                ignored["subfolders"] += 1
                continue
            if not stat.S_ISREG(st.st_mode) or not name.lower().endswith(EXTENSIONS):
                ignored["other_files"] += 1
                continue
        except OSError:
            unreadable.append(name)
            continue
        if _late(deadline):
            partial = True
            break
        key = outdir.norm(path)
        known = cached.get(key)
        if (isinstance(known, list) and len(known) == 3 and known[0] == st.st_size
                and known[1] == st.st_mtime_ns and _SHA1_RE.match(str(known[2]))):
            sha1 = known[2]
        else:
            sha1, why = _hash_file(path)
            if sha1 is None:
                unreadable.append(name)
                continue
        fresh[key] = [st.st_size, st.st_mtime_ns, sha1]
        files.append({"name": name, "key": key, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha1": sha1})
    return {"ok": True, "files": files, "unreadable": unreadable, "ignored": ignored, "partial": partial,
            "hashes": fresh}


# --------------------------------------------------------------------------- Modrinth (through net.py only)

def _api(method, path, params=None, body=None):
    return net.fetch("modrinth", BASE + path, method=method, params=params, json_body=body, want="json")


def _chunks(items, size):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _good_version(version):
    """A Modrinth version object that has what this check reads, else None."""
    if not isinstance(version, dict):
        return None
    vid, pid = version.get("id"), version.get("project_id")
    if not (isinstance(vid, str) and _ID_RE.match(vid) and isinstance(pid, str) and _ID_RE.match(pid)):
        return None
    if not isinstance(version.get("files"), list):
        return None
    return version


def _file_with_hash(version, sha1):
    for item in version.get("files") or ():
        hashes = item.get("hashes") if isinstance(item, dict) else None
        if isinstance(hashes, dict) and str(hashes.get("sha1") or "").lower() == sha1:
            return item
    return None


def _primary_file(version):
    items = [f for f in (version.get("files") or ()) if isinstance(f, dict)]
    for item in items:
        if item.get("primary"):
            return item
    return items[0] if items else None


def match_hashes(hashes, deadline=None):
    """Who do these SHA-1 hashes belong to?  -> ({hash: version}, failure | None, {hash: failure}).

    A hash is only matched when the version Modrinth names really lists that hash among its own files."""
    matched, errors, first_failure = {}, {}, None
    todo = sorted(set(hashes))
    for batch in _chunks(todo, HASH_BATCH):
        if first_failure is not None or _late(deadline):
            why = first_failure or fail(OFFLINE, "The time allowed for this check ran out.")
            for h in batch:
                errors[h] = why
            continue
        res = _api("POST", "/version_files", body={"hashes": batch, "algorithm": "sha1"})
        if not res.get("ok"):
            first_failure = res
            for h in batch:
                errors[h] = res
            continue
        body = res.get("body")
        if not isinstance(body, dict):
            first_failure = fail(VENDOR_CHANGED, "Modrinth answered, but not in the form Jarvis expects. The site may have changed.")
            for h in batch:
                errors[h] = first_failure
            continue
        for h in batch:
            version = _good_version(body.get(h))
            if version is not None and _file_with_hash(version, h) is not None:
                matched[h] = version
    return matched, first_failure, errors


def _latest_for(hashes, loaders, mc_versions, deadline):
    """`POST /version_files/update`: the newest version per hash for these loaders and Minecraft versions.
    -> ({hash: version}, {hash: failure})."""
    found, errors = {}, {}
    for batch in _chunks(sorted(set(hashes)), HASH_BATCH):
        if _late(deadline):
            for h in batch:
                errors[h] = fail(OFFLINE, "The time allowed for this check ran out.")
            continue
        res = _api("POST", "/version_files/update",
                   body={"hashes": batch, "algorithm": "sha1", "loaders": list(loaders), "game_versions": list(mc_versions)})
        if not res.get("ok"):
            for h in batch:
                errors[h] = res
            continue
        body = res.get("body")
        if not isinstance(body, dict):
            for h in batch:
                errors[h] = fail(VENDOR_CHANGED, "Modrinth answered, but not in the form Jarvis expects.")
            continue
        for h in batch:
            version = _good_version(body.get(h))
            if version is not None:
                found[h] = version
    return found, errors


def _newest_acceptable(project_id, loaders, mc_versions, accept, memo, deadline):
    """The newest version of one project on an acceptable channel -> (version | None, failure | None).  Memoised."""
    key = (project_id, tuple(loaders), tuple(mc_versions), accept)
    if key in memo:
        return memo[key]
    if _late(deadline):
        out = (None, fail(OFFLINE, "The time allowed for this check ran out."))
    else:
        res = _api("GET", "/project/%s/version" % project_id,
                   params={"loaders": json.dumps(list(loaders), separators=(",", ":")),
                           "game_versions": json.dumps(list(mc_versions), separators=(",", ":"))})
        body = res.get("body") if res.get("ok") else None
        if not res.get("ok"):
            out = (None, res)
        elif not isinstance(body, list):
            out = (None, fail(VENDOR_CHANGED, "Modrinth answered, but not in the form Jarvis expects."))
        else:
            good = [v for v in (_good_version(x) for x in body) if v is not None and _rank(v.get("version_type")) <= accept
                    and _when(v.get("date_published")) is not None]
            good.sort(key=lambda v: _when(v.get("date_published")), reverse=True)
            out = (good[0] if good else None, None)
    memo[key] = out
    return out


def project_names(project_ids, deadline=None):
    """{project id: {"title", "slug"}} (best effort: a failure just means no names)."""
    out = {}
    for batch in _chunks(sorted({p for p in project_ids if isinstance(p, str) and _ID_RE.match(p)}), PROJECT_BATCH):
        if _late(deadline):
            break
        res = _api("GET", "/projects", params={"ids": json.dumps(batch, separators=(",", ":"))})
        body = res.get("body") if res.get("ok") else None
        if not isinstance(body, list):
            continue
        for item in body:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                out[item["id"]] = {"title": untrusted.clean_name(item.get("title") or ""),
                                   "slug": untrusted.clean(item.get("slug") or "", 60)}
    return out


# --------------------------------------------------------------------------- the decision for one file

def _versions_text(versions):
    shown = list(versions)[:MAX_LISTED_VERSIONS]
    return ", ".join(shown) + (" ..." if len(versions) > len(shown) else "")


def _filters_for(installed, mc_arg, loader_arg, quilt_fallback):
    """The Minecraft versions and loaders to look for, for one matched file.

    -> (mc_versions, loaders, None) | (None, None, (status, why, detail)).  Never guesses: the owner's answer, else
    the file's own listing when it names exactly one, else a question (`needs_*`)."""
    inst_mc = _strings(installed.get("game_versions"))
    inst_ld = [x.lower() for x in _strings(installed.get("loaders"))]

    if mc_arg:
        if mc_arg not in inst_mc:
            return None, None, ("other_version", "other_minecraft_version",
                                "This file is built for Minecraft %s, not %s, so no update was looked up for it."
                                % (_versions_text(inst_mc) or "an unknown version", mc_arg))
        mcs = [mc_arg]
    elif len(inst_mc) == 1:
        mcs = inst_mc
    elif not inst_mc:
        return None, None, ("not_checked", "no_minecraft_version", "Modrinth lists no Minecraft version for this file.")
    else:
        return None, None, ("not_checked", "needs_mc_version",
                            "This file works on several Minecraft versions (%s). Say which one you play." % _versions_text(inst_mc))

    mod_loader = bool(set(inst_ld) & set(sources.LOADERS))
    if not mod_loader:
        if not inst_ld:
            return None, None, ("not_checked", "no_loader", "Modrinth lists no loader for this file.")
        return mcs, inst_ld, None                          # a resource pack / shader / data pack: its own listing
    fallback = bool(loader_arg == "quilt" and quilt_fallback)
    if loader_arg:
        if loader_arg not in inst_ld and not (fallback and "fabric" in inst_ld):
            return None, None, ("other_version", "other_loader",
                                "This file is built for %s, not %s, so no update was looked up for it."
                                % (", ".join(inst_ld), loader_arg))
        return mcs, (["quilt", "fabric"] if fallback else [loader_arg]), None
    if len(inst_ld) == 1:
        return mcs, inst_ld, None
    return None, None, ("not_checked", "needs_loader",
                        "This file works with several loaders (%s). Say which one you use." % ", ".join(inst_ld))


def _decide(rec, latest, loaders, accept, memo, deadline):
    """Fill rec's status from the installed version and the newest one `/version_files/update` named."""
    installed = rec["installed"]
    candidate = latest
    if candidate is not None and _rank(candidate.get("version_type")) > accept:
        candidate, bad = _newest_acceptable(installed["project_id"], loaders, rec["mcs"], accept, memo, deadline)
        if bad is not None:
            return _not_checked(rec, bad.get("reason") or OFFLINE, bad.get("error") or "Modrinth did not answer.")
    if candidate is None:
        return _not_checked(rec, VENDOR_CHANGED, "Modrinth named no file for this project on that Minecraft version and loader.")
    if candidate["id"] == installed["id"]:
        rec["status"] = "up_to_date"
        return rec
    a, b = _when(candidate.get("date_published")), _when(installed.get("date_published"))
    if a is None or b is None:
        return _not_checked(rec, VENDOR_CHANGED, "Modrinth gave no publication date to compare, so no verdict was made.")
    if a <= b:
        rec["status"] = "up_to_date"
        return rec
    rec["status"] = "update_available"
    rec["latest"] = candidate
    if "quilt" in loaders and "fabric" in loaders and "quilt" not in _strings(candidate.get("loaders")):
        rec["via"] = "fabric"
    return rec


def _not_checked(rec, reason, detail):
    rec["status"] = "not_checked"
    rec["why"] = reason if isinstance(reason, str) and reason else VENDOR_CHANGED
    rec["detail"] = str(detail)[:200]
    return rec


# --------------------------------------------------------------------------- the check

def check_folder(folder=None, default_folder="~/Downloads", mc_version=None, loader=None, channel="release",
                 quilt_fallback=True, deadline_seconds=0):
    """Do the mods in one folder have newer compatible files?  See the module docstring.  Never raises."""
    try:
        return _check(folder, default_folder, mc_version, loader, channel, quilt_fallback, deadline_seconds)
    except Exception as exc:  # noqa: BLE001 - this never raises
        return fail(UNSUPPORTED, "The update check failed (%s). Nothing was changed." % type(exc).__name__)


def _check(folder, default_folder, mc_arg, loader_arg, channel, quilt_fallback, deadline_seconds):
    channel = str(channel or "release").lower()
    if channel not in CHANNELS:
        return fail(UNSUPPORTED, "channel must be release, beta or alpha.")
    try:
        seconds = float(deadline_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    deadline = time.monotonic() + seconds if seconds > 0 else None

    state = load_state()
    path, how, bad = resolve_folder(folder, default_folder, state)
    if bad:
        return bad
    scanned = scan_folder(path, state["hashes"], deadline)
    if not scanned.get("ok"):
        return scanned

    # Remember what was hashed (and the folder), dropping entries of this folder that no longer exist.
    prefix = outdir.norm(path).rstrip("\\/") + os.sep
    kept = {k: v for k, v in state["hashes"].items() if not k.startswith(prefix)}
    kept.update(scanned["hashes"])
    save_state({"last_folder": str(path), "hashes": kept})

    texts = []
    out = {"ok": True, "source": "modrinth", "folder": str(path), "folder_from": how, "nothing_changed": True,
           "ignored": scanned["ignored"]}
    if mc_arg:
        out["mc_version"] = mc_arg
    if loader_arg:
        out["loader"] = loader_arg
    out["channel"] = channel
    files = scanned["files"]
    if scanned["unreadable"]:
        out["unreadable"] = [untrusted.clean_file_name(n) for n in scanned["unreadable"]]
    if not files:
        out.update({"checked": 0, "updates_available": 0, "updates": [], "up_to_date": [], "not_checked": []})
        if scanned["partial"]:
            out["partial"] = True
            out["message"] = ("The check stopped early (the time allowed ran out or the folder is very large) before any "
                              "file in %s was read, so nothing was checked. Nothing was changed." % path)
        else:
            out["message"] = ("No .jar, .zip or .mrpack files were found directly in %s, so there is nothing to check. "
                              "Sub-folders are not entered. Nothing was changed." % path)
        return out

    matched, first_failure, errors = match_hashes([f["sha1"] for f in files], deadline)
    if first_failure is not None and not matched and len(errors) == len({f["sha1"] for f in files}):
        return fail(first_failure.get("reason") or OFFLINE,
                    "Couldn't ask Modrinth about these files: %s" % (first_failure.get("error") or "no answer"))

    records = []
    for item in files:
        rec = {"name": item["name"], "sha1": item["sha1"], "size": item["size"], "status": None}
        version = matched.get(item["sha1"])
        if version is None:
            err = errors.get(item["sha1"])
            if err is not None:
                _not_checked(rec, err.get("reason") or OFFLINE, err.get("error") or "Modrinth did not answer.")
            else:
                _not_checked(rec, "not_on_modrinth", "Modrinth does not know this file (by its hash).")
        else:
            rec["installed"] = version
            texts.extend([str(version.get("name") or ""), str(version.get("version_number") or "")])
        records.append(rec)

    # Group the matched files by what to look for, then one request per group.
    groups = {}
    for rec in records:
        if rec["status"] is not None:
            continue
        mcs, loaders, problem = _filters_for(rec["installed"], mc_arg, loader_arg, quilt_fallback)
        if problem is not None:
            status, why, detail = problem
            rec["status"] = status
            rec["why"], rec["detail"] = why, detail
            continue
        rec["mcs"], rec["loaders"] = mcs, loaders
        groups.setdefault((tuple(mcs), tuple(loaders)), []).append(rec)

    memo = {}
    for (mcs, loaders), recs in groups.items():
        latest, errs = _latest_for([r["sha1"] for r in recs], loaders, mcs, deadline)
        for rec in recs:
            if rec["sha1"] in errs:
                err = errs[rec["sha1"]]
                _not_checked(rec, err.get("reason") or OFFLINE, err.get("error") or "Modrinth did not answer.")
                continue
            if rec["sha1"] not in latest:
                _not_checked(rec, VENDOR_CHANGED, "Modrinth named no file for this one on that Minecraft version and loader.")
                continue
            accept = max(_RANK[channel], _rank(rec["installed"].get("version_type"), _RANK[channel]))
            _decide(rec, latest[rec["sha1"]], list(loaders), accept, memo, deadline)
            if rec["status"] == "update_available":
                texts.extend([str(rec["latest"].get("name") or ""), str(rec["latest"].get("version_number") or "")])

    names = project_names([r["installed"]["project_id"] for r in records if "installed" in r], deadline)
    texts.extend(info.get("title") or "" for info in names.values())
    if _late(deadline):
        scanned["partial"] = True
    return _assemble(out, records, names, texts, scanned)


# --------------------------------------------------------------------------- the answer

def _project_block(rec, names):
    pid = rec["installed"]["project_id"]
    info = names.get(pid) or {}
    block = {"id": pid, "page_url": "https://modrinth.com/project/%s" % pid}
    if info.get("title"):
        block["name"] = info["title"]
    if info.get("slug"):
        block["slug"] = info["slug"]
    return block


def _assemble(out, records, names, texts, scanned):
    updates, current, unchecked, other = [], [], [], []
    for rec in records:
        shown = untrusted.clean_file_name(rec["name"])
        if rec["status"] == "update_available":
            installed, latest = rec["installed"], rec["latest"]
            file_info = _primary_file(latest) or {}
            entry = {"file": shown, "project": _project_block(rec, names),
                     "installed": {"version": untrusted.clean(installed.get("version_number") or "", 60),
                                   "channel": str(installed.get("version_type") or ""),
                                   "date": _day(installed.get("date_published"))},
                     "latest": {"version": untrusted.clean(latest.get("version_number") or "", 60),
                                "file_id": latest["id"], "channel": str(latest.get("version_type") or ""),
                                "date": _day(latest.get("date_published")),
                                "file_name": untrusted.clean_file_name(file_info.get("filename") or ""),
                                "size": file_info.get("size") if isinstance(file_info.get("size"), int) else None},
                     "get_with": {"tool": "download_mc_file", "source": "modrinth",
                                  "project": rec["installed"]["project_id"], "file": latest["id"]}}
            if rec.get("via"):
                entry["latest"]["via"] = rec["via"]
            updates.append(entry)
        elif rec["status"] == "up_to_date":
            current.append({"file": shown, "project": (_project_block(rec, names).get("name")
                                                      or rec["installed"]["project_id"]),
                            "version": untrusted.clean(rec["installed"].get("version_number") or "", 60)})
        elif rec["status"] == "other_version":
            other.append({"file": shown, "why": rec.get("why"), "detail": rec.get("detail")})
        else:
            unchecked.append({"file": shown, "why": rec.get("why") or VENDOR_CHANGED, "detail": rec.get("detail") or ""})

    out.update({"checked": len(updates) + len(current), "updates_available": len(updates), "updates": updates,
                "up_to_date": current, "not_checked": unchecked})
    if other:
        out["other_version"] = other
    if scanned.get("partial"):
        out["partial"] = True
    if any(u["why"] in ("not_on_modrinth",) for u in unchecked):
        out["curseforge_note"] = CURSEFORGE_NOTE
    ask = sorted({u["why"] for u in unchecked if u["why"] in ("needs_mc_version", "needs_loader")})
    if ask:
        out["needs_clarification"] = True
        out["ask"] = " and ".join({"needs_mc_version": "which Minecraft version you play",
                                   "needs_loader": "which loader you use (fabric, forge, neoforge or quilt)"}[a] for a in ask)
    bits = ["%d of %d file(s) checked against Modrinth have a newer compatible file" % (len(updates), len(updates) + len(current)),
            "%d are up to date" % len(current)]
    if unchecked:
        bits.append("%d could not be checked (reasons listed)" % len(unchecked))
    if other:
        bits.append("%d are built for another Minecraft version or loader" % len(other))
    message = "In %s: %s. " % (out["folder"], "; ".join(bits))
    if out.get("partial"):
        message += "The check stopped early (the time allowed ran out or the folder is very large), so the list is partial. "
    message += ("Nothing was downloaded, replaced, updated or installed. To get a newer file, ask for it with "
                "download_mc_file (it queues it in Downloads and leaves the old file alone).")
    out["message"] = message
    return untrusted.mark(out, *texts)
