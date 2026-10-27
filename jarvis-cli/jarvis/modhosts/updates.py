"""Update checks for the Minecraft files in ONE folder the owner names (brief S2).  Nothing is ever changed.

The owner asks "do my mods have newer files?"; this answers with a list.  It never downloads, replaces, renames or
installs anything: it reads the folder, asks Modrinth, and reports.  (To fetch a newer file the owner asks for it
with `download_mc_file`, which queues it in Downloads and never touches the old jar.)

HOW A FILE IS REMEMBERED (the open design question)
  It is not.  The folder IS the record: every .jar / .zip / .mrpack in it is hashed (SHA-1) and Modrinth is asked who
  those hashes belong to (`POST /version_files`).  A file is matched to a project by its CONTENT, never by its name, so a
  renamed jar still matches and a look-alike name never does.  The only thing kept on disk is a small state file,
  ~/.jarvis/modhosts/update_state.json: a hash cache (path, size, modified time -> SHA-1 and, once a file was looked
  up on CurseForge, its CurseForge fingerprint, so an unchanged jar is not read again) and the last folder checked.  It
  holds no key, no vendor answer and nothing that cannot be rebuilt; losing it costs one re-hash.  `hub/` is not touched and no other module's rows are read.

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
  * A file Modrinth does not know is "not checked", never guessed, unless CurseForge knows it EXACTLY.  CurseForge
    identifies files by its own fingerprint (a murmur2 hash of the file, computed here by crossmatch.fingerprint_file
    on this PC, nothing downloaded) and a file counts as matched only when the fingerprint AND the SHA-1 both agree; a
    name or a size is never evidence.  Only fingerprints (numbers) leave the PC, to api.curseforge.com, through the
    adapter (curseforge.fingerprint_matches / list_files / bulk_projects): this module never calls net for CurseForge and
    never sees the key.  The fingerprint is cached next to the SHA-1 in the state file (same size + modified time rule).
  * THE CURSEFORGE ROUTE IS UNVERIFIED: the fingerprint lookup and the murmur2 variant were never tried against a real
    CurseForge file (modhosts/crossmatch.py says so).  Before any answer is trusted, the cheap sanity question of
    crossmatch is asked (a fingerprint nobody has).  A changed or odd answer marks the whole CurseForge half "not
    checked" (vendor_changed); the Modrinth half is unaffected.  No key skips that half with ONE hint (why no_key).
    A project whose author blocks third-party downloads is listed as not_distributable, never routed around.
  * With no folder named and no last folder the check does NOT quietly read ~/Downloads: it answers needs_folder with an
    ask line that suggests the default_dest setting (nothing is read, hashed or sent).
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

from . import AMBIGUOUS, BAD_DEST, NOT_DISTRIBUTABLE, OFFLINE, UNSUPPORTED, VENDOR_CHANGED, fail, net, outdir, sources, untrusted

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

FP_MAX = 0xFFFFFFFF              # a CurseForge fingerprint is an unsigned 32 bit number
CF_FATAL = ("no_key", "bad_key", "rate_limited", "offline")     # a failure that would hit every CurseForge call
CF_REASONS = CF_FATAL + ("vendor_changed", "unsupported")

# The ONE hint about CurseForge (replaces the old "not computed" text).  Each says what happened and that nothing was guessed.
CURSEFORGE_NOTE = ("A file Modrinth does not know may have come from CurseForge. CurseForge identifies its files by their "
                   "own fingerprint, which Jarvis computes on this PC without sending the file anywhere, but asking "
                   "CurseForge needs your own CurseForge key and none is set (add one in Settings > Integrations > "
                   "Minecraft mods). So those files were not checked on CurseForge. Nothing was guessed from a file name "
                   "or size; the Modrinth results are complete.")
CF_NOTE_RAN = ("Files Modrinth does not know were looked up on CurseForge by their fingerprint, a number computed on this "
               "PC (only those numbers were sent, to api.curseforge.com). This CurseForge route is UNVERIFIED: it has never "
               "been tried against a real CurseForge file, so treat these results with care. A file counts as matched only "
               "when its fingerprint AND its SHA-1 both agree; anything else is not checked, never guessed from a name or size.")
CF_NOTE_CHANGED = ("CurseForge's fingerprint lookup answered in a way Jarvis does not recognise (this route is UNVERIFIED: it "
                   "has never been tried against a real CurseForge file), so the whole CurseForge half was not checked. The "
                   "Modrinth results are unaffected. Nothing was guessed.")
CF_NOTE_OTHER = ("The CurseForge half was not checked: %s. The Modrinth results are unaffected. Nothing was guessed from a "
                 "file name or size.")
CF_WHY_TEXT = {
    "bad_key": "CurseForge refused the key (it is wrong, expired or revoked, or is not allowed the fingerprint route)",
    "rate_limited": "CurseForge asked Jarvis to slow down (too many calls); try again in a little while",
    "offline": "CurseForge did not answer (the network is down or its server had an error)",
    "unsupported": "CurseForge lookups by fingerprint are not available in this build",
    "unreadable": "none of the files could be read to compute their fingerprint",
}


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


def _good_fp(value):
    """A CurseForge fingerprint as it may be remembered: an int from 0 to 2**32 - 1 (a bool is not one)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= FP_MAX


def _crossmatch():
    """The module that computes CurseForge's fingerprint (read-only for this file), or None when it is not usable."""
    try:
        from . import crossmatch
    except Exception:  # noqa: BLE001
        return None
    return crossmatch if callable(getattr(crossmatch, "fingerprint_file", None)) else None


# --------------------------------------------------------------------------- the state file

def state_path():
    """Resolved at call time so a test that redirects HOME first sees its own folder."""
    return Path.home() / ".jarvis" / "modhosts" / "update_state.json"


def load_state():
    """{"last_folder": str | None, "hashes": {key: [size, mtime_ns, sha1] or [size, mtime_ns, sha1, fingerprint]}};
    anything unreadable is an empty state (a fingerprint that is not a number is dropped, the SHA-1 stays)."""
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
            if (isinstance(key, str) and isinstance(value, list) and len(value) in (3, 4)
                    and isinstance(value[0], int) and isinstance(value[1], int)
                    and isinstance(value[2], str) and _SHA1_RE.match(value[2])):
                hashes[key] = value[:4] if len(value) == 4 and _good_fp(value[3]) else value[:3]
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
        text = json.dumps({"v": 2, "last_folder": state.get("last_folder"), "hashes": hashes},
                          separators=(",", ":"), ensure_ascii=False)
        while len(text.encode("utf-8")) > STATE_MAX_BYTES and hashes:
            for _ in range(max(1, len(hashes) // 10)):
                hashes.pop(next(iter(hashes)))
            text = json.dumps({"v": 2, "last_folder": state.get("last_folder"), "hashes": hashes},
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

    -> {"ok": True, "files": [{"name", "path", "key", "size", "mtime_ns", "sha1", "fingerprint"}], "unreadable": [name],
        "ignored": {...}, "partial": bool, "hashes": {key: [size, mtime_ns, sha1(, fingerprint)]}}  |  a failure.
    `fingerprint` is the remembered CurseForge fingerprint of an unchanged file, else None (it is computed only when needed)."""
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
        fingerprint = None
        if (isinstance(known, list) and len(known) in (3, 4) and known[0] == st.st_size
                and known[1] == st.st_mtime_ns and _SHA1_RE.match(str(known[2]))):
            sha1 = known[2]
            if len(known) == 4 and _good_fp(known[3]):
                fingerprint = known[3]
        else:
            sha1, why = _hash_file(path)
            if sha1 is None:
                unreadable.append(name)
                continue
        fresh[key] = [st.st_size, st.st_mtime_ns, sha1] + ([fingerprint] if fingerprint is not None else [])
        files.append({"name": name, "path": path, "key": key, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                      "sha1": sha1, "fingerprint": fingerprint})
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


def _filters_for(installed, mc_arg, loader_arg, quilt_fallback, site="Modrinth", loaderless_ok=False):
    """The Minecraft versions and loaders to look for, for one matched file.  `site` names who listed the file in a
    sentence; `loaderless_ok` (CurseForge resource packs, shaders and data packs carry no loader tag) means a file with no
    loader at all is looked up by Minecraft version alone (loaders []).

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
        return None, None, ("not_checked", "no_minecraft_version", "%s lists no Minecraft version for this file." % site)
    else:
        return None, None, ("not_checked", "needs_mc_version",
                            "This file works on several Minecraft versions (%s). Say which one you play." % _versions_text(inst_mc))

    mod_loader = bool(set(inst_ld) & set(sources.LOADERS))
    if not mod_loader:
        if not inst_ld:
            if loaderless_ok:
                return mcs, [], None
            return None, None, ("not_checked", "no_loader", "%s lists no loader for this file." % site)
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


# --------------------------------------------------------------------------- CurseForge (through the adapter only)
#
# Files Modrinth does not know are looked up on CurseForge by CurseForge's own fingerprint.  UNVERIFIED route (see the
# module docstring): everything here answers a doubt with "not checked", never with a guess.

def _cf_problem(answer, sanity=False):
    """None when `answer` (fingerprint_matches) is usable, else the reason code to stop the CurseForge half with."""
    if not isinstance(answer, dict):
        return VENDOR_CHANGED
    if not answer.get("ok"):
        reason = answer.get("reason")
        return reason if reason in CF_REASONS else VENDOR_CHANGED
    matches = answer.get("matches")
    if not isinstance(matches, dict):
        return VENDOR_CHANGED
    if sanity and matches:                                    # the fingerprint nobody has was "found": not believable
        return VENDOR_CHANGED
    return None


def _cf_detail(why, error=None):
    if why == VENDOR_CHANGED:
        return ("CurseForge's fingerprint answer could not be trusted (this route is unverified), so this file was not "
                "checked on CurseForge.")
    text = CF_WHY_TEXT.get(why)
    if text:
        return ("%s; this file was not checked on CurseForge." % text)[:200]
    return ("CurseForge could not be asked about this file (%s)." % untrusted.clean(error or why, 80))[:200]


def _cf_mark(rec, why, detail):
    _not_checked(rec, why, detail)
    rec["extra"] = {"source": "curseforge"}


def _cf_skip(info, why, recs):
    """The whole CurseForge half is not checked.  With no key (or no adapter) the files keep their plain
    `not_on_modrinth` and the ONE hint explains; with any other reason each file still waiting says why."""
    info["status"], info["why"] = "skipped", why
    if why not in ("no_key", "unsupported"):
        for rec in recs:
            if rec["status"] == "not_checked" and rec.get("why") == "not_on_modrinth":     # still waiting for a verdict
                _cf_mark(rec, why, _cf_detail(why))


def _fingerprint_of(item, cm):
    """CurseForge's fingerprint of one scanned file -> int | None.  The remembered one when the file is unchanged;
    else computed on this PC (nothing downloaded) after re-checking the file is still the plain file that was hashed:
    not a link, same size and modified time, and the SHA-1 of the very same pass must equal the one already known."""
    if _good_fp(item.get("fingerprint")):
        return item["fingerprint"]
    path = item.get("path")
    try:
        if not path or outdir.is_link(path):
            return None
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size != item["size"] or st.st_mtime_ns != item["mtime_ns"]:
            return None
        got = cm.fingerprint_file(path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(got, dict) or not got.get("ok") or got.get("sha1") != item["sha1"] or got.get("size") != item["size"]:
        return None
    return got["fingerprint"] if _good_fp(got.get("fingerprint")) else None


def _remember_fingerprint(kept, item, fingerprint):
    """Put the fingerprint next to the SHA-1 in the hash cache.  True when the cache changed."""
    entry = kept.get(item["key"])
    if (isinstance(entry, list) and len(entry) in (3, 4) and entry[2] == item["sha1"]
            and not (len(entry) == 4 and entry[3] == fingerprint)):
        kept[item["key"]] = list(entry[:3]) + [fingerprint]
        return True
    return False


def _curseforge_half(records, kept, mc_arg, loader_arg, channel, quilt_fallback, deadline, texts):
    """Look the files Modrinth does not know up on CurseForge.  Changes those records in place.
    -> None (no such file: CurseForge is not asked) | {"status": "checked" | "skipped", "why"?, "verified": False, ...,
    "state_changed": bool}.  Never raises: an unexpected failure only leaves the files as they were."""
    unknown = [r for r in records if r["status"] == "not_checked" and r.get("why") == "not_on_modrinth"]
    if not unknown:
        return None
    info = {"status": "skipped", "route": "fingerprint", "verified": False, "asked": 0, "matched": 0, "state_changed": False}
    try:
        _cf_run(unknown, info, kept, mc_arg, loader_arg, channel, quilt_fallback, deadline, texts)
    except Exception:  # noqa: BLE001 - a bug here must never cost the Modrinth half
        info["status"], info["why"] = "skipped", "unsupported"
    return info


def _cf_run(unknown, info, kept, mc_arg, loader_arg, channel, quilt_fallback, deadline, texts):
    adapter = sources.get("curseforge")
    cm = _crossmatch()
    finder = getattr(adapter, "fingerprint_matches", None) if adapter is not None else None
    if not callable(finder) or cm is None or not callable(getattr(adapter, "list_files", None)) \
            or not callable(getattr(adapter, "bulk_projects", None)):
        return _cf_skip(info, UNSUPPORTED, unknown)

    # 1. crossmatch's cheap sanity question (a fingerprint nobody has) BEFORE any file is read.  It also answers "no key"
    #    without a request, so a missing key costs no file read at all.
    why = _cf_problem(finder([getattr(adapter, "FINGERPRINT_PROBE", 1)]), sanity=True)
    if why:
        return _cf_skip(info, why, unknown)

    # 2. the fingerprints, computed here and remembered next to the SHA-1
    fingerprints, pending = {}, []
    for rec in unknown:
        if _late(deadline):
            _cf_mark(rec, OFFLINE, "The time allowed for this check ran out.")
            continue
        sha1 = rec["sha1"]
        if sha1 not in fingerprints:
            fingerprints[sha1] = _fingerprint_of(rec["item"], cm)
        fp = fingerprints[sha1]
        if fp is None:
            _cf_mark(rec, "unreadable", "The file changed or could not be read while its CurseForge fingerprint was worked out.")
            continue
        if _remember_fingerprint(kept, rec["item"], fp):
            info["state_changed"] = True
        rec["fp"] = fp
        pending.append(rec)
    if not pending:
        info["why"] = "unreadable"
        return None

    # 3. one lookup for all of them (the adapter chunks it); only the numbers leave the PC
    asked = sorted({r["fp"] for r in pending})
    info["asked"] = len(asked)
    answer = finder(asked)
    why = _cf_problem(answer)
    if why:
        return _cf_skip(info, why, pending)
    info["status"] = "checked"

    # 4. an EXACT match: fingerprint and SHA-1 both agree; two different files that fit are ambiguous
    matched = []
    for rec in pending:
        candidates, differing = {}, 0
        for record in answer["matches"].get(rec["fp"]) or []:
            if not isinstance(record, dict):
                continue
            hashes = record.get("hashes") if isinstance(record.get("hashes"), dict) else {}
            stamp = str(hashes.get("fingerprint") or "")
            if stamp and stamp != str(rec["fp"]):
                continue
            if str(hashes.get("sha1") or "").lower() == rec["sha1"] and str(record.get("project_id") or "").isdigit() \
                    and str(record.get("file_id") or "").isdigit():
                candidates[(record["project_id"], record["file_id"])] = record
            else:
                differing += 1
        if not candidates:
            _cf_mark(rec, "not_on_curseforge",
                     "Neither Modrinth nor CurseForge knows this file (by its hash, and by its CurseForge fingerprint with an "
                     "identical SHA-1)." + (" CurseForge has a file with the same fingerprint but another SHA-1, which is "
                                            "not used." if differing else ""))
        elif len(candidates) > 1:
            _cf_mark(rec, AMBIGUOUS, "More than one CurseForge file is identical to it; Jarvis doesn't pick between them.")
        else:
            rec["cf"] = {"installed": next(iter(candidates.values()))}
            matched.append(rec)
    info["matched"] = len(matched)
    if not matched:
        return None

    # 5. names, page links, the kind of project and whether its author allows third-party downloads
    projects = {}
    got = adapter.bulk_projects(sorted({r["cf"]["installed"]["project_id"] for r in matched}))
    if isinstance(got, dict) and got.get("ok") and isinstance(got.get("projects"), dict):
        projects = {k: v for k, v in got["projects"].items() if isinstance(v, dict)}
    elif isinstance(got, dict) and got.get("reason") in CF_FATAL:
        return _cf_skip(info, got["reason"], matched)

    # 6. per file: which Minecraft version and loader, then the newer-by-date decision (the Modrinth path's rules)
    memo, fatal = {}, None
    for rec in matched:
        installed = rec["cf"]["installed"]
        project = projects.get(installed["project_id"])
        rec["cf"]["project"] = project
        texts.extend([str(installed.get("version_label") or ""), str((project or {}).get("name") or "")])
        loaderless = bool(project) and project.get("kind") in ("resourcepack", "shader", "datapack")
        mcs, loaders, problem = _filters_for({"game_versions": installed.get("mc_versions"), "loaders": installed.get("loaders")},
                                             mc_arg, loader_arg, quilt_fallback, "CurseForge", loaderless)
        if problem is not None:
            rec["status"], rec["why"], rec["detail"] = problem
            rec["extra"] = {"source": "curseforge"}
            continue
        if fatal is not None:
            _cf_mark(rec, fatal["reason"], _cf_detail(fatal["reason"], fatal.get("error")))
            continue
        if _late(deadline):
            _cf_mark(rec, OFFLINE, "The time allowed for this check ran out.")
            continue
        key = (installed["project_id"], mcs[0], tuple(loaders))
        if key not in memo:
            # one loader: the adapter filters it exactly; Quilt with its Fabric fallback, or no loader at all: all files
            # come back and are picked below (the vendor's own loader filter is loose, so it is never relied on alone)
            memo[key] = adapter.list_files(installed["project_id"], mcs[0], loaders[0] if len(loaders) == 1 else None)
            if isinstance(memo[key], dict) and not memo[key].get("ok") and memo[key].get("reason") in CF_FATAL:
                fatal = memo[key]
        listing = memo[key]
        if not isinstance(listing, dict) or not listing.get("ok") or not isinstance(listing.get("files"), list):
            reason = listing.get("reason") if isinstance(listing, dict) and listing.get("reason") in CF_REASONS + ("not_found",) \
                else VENDOR_CHANGED
            _cf_mark(rec, reason, _cf_detail(reason, listing.get("error") if isinstance(listing, dict) else None))
            continue
        rec["mcs"], rec["loaders"] = mcs, loaders
        accept = max(_RANK[channel], _rank(installed.get("channel"), _RANK[channel]))
        _decide_cf(rec, listing["files"], accept)
        rec["extra"] = dict(rec.get("extra") or {}, source="curseforge")
        if rec["status"] == "update_available":
            latest = rec["cf"]["latest"]
            texts.extend([str(latest.get("version_label") or ""), str(latest.get("file_name") or "")])
    if fatal is not None:
        info["stopped_by"] = fatal["reason"]


def _decide_cf(rec, files, accept):
    """Fill rec's status from the installed CurseForge file and the project's file list (newest first by date)."""
    installed, mc, loaders = rec["cf"]["installed"], rec["mcs"][0], rec["loaders"]
    pool = []
    for item in files:
        if not isinstance(item, dict) or not str(item.get("file_id") or "").isdigit() or item.get("why") == "unavailable":
            continue
        if _rank(item.get("channel")) > accept or mc not in _strings(item.get("mc_versions")):
            continue
        if loaders and not (set(x.lower() for x in _strings(item.get("loaders"))) & set(loaders)):
            continue
        if _when(item.get("date")) is None:
            continue
        pool.append(item)
    if not pool:
        return _not_checked(rec, VENDOR_CHANGED, "CurseForge named no file for this project on that Minecraft version and loader.")
    pool.sort(key=lambda f: (_when(f["date"]), int(f["file_id"])), reverse=True)
    candidate = pool[0]
    if candidate["file_id"] == installed["file_id"]:
        rec["status"] = "up_to_date"
        return rec
    a, b = _when(candidate.get("date")), _when(installed.get("date"))
    if b is None:
        return _not_checked(rec, VENDOR_CHANGED, "CurseForge gave no publication date to compare, so no verdict was made.")
    if a <= b:
        rec["status"] = "up_to_date"
        return rec
    rec["cf"]["latest"] = candidate
    project = rec["cf"].get("project") or {}
    if project.get("distributable") is False or candidate.get("why") == "no_url":
        # the author's choice: listed, and never worked around (no download link is asked for, none is built)
        _not_checked(rec, NOT_DISTRIBUTABLE,
                     "A newer file exists on CurseForge (%s, %s) but its author doesn't allow third-party downloads, so "
                     "Jarvis can't fetch it. Open the project page to get it yourself." % (
                         untrusted.clean(candidate.get("version_label") or candidate.get("file_name") or "", 60),
                         _day(candidate.get("date"))))
        return rec
    if not candidate.get("downloadable"):
        return _not_checked(rec, VENDOR_CHANGED, "The newer CurseForge file has no download link Jarvis can use.")
    rec["status"] = "update_available"
    if "quilt" in rec["loaders"] and "fabric" in rec["loaders"] and "quilt" not in _strings(candidate.get("loaders")):
        rec["via"] = "fabric"
    return rec


def _cf_note(info):
    """The ONE hint about CurseForge for this answer."""
    why = info.get("why")
    if info.get("status") == "checked":
        return CF_NOTE_RAN
    if why == "no_key":
        return CURSEFORGE_NOTE
    if why == VENDOR_CHANGED:
        return CF_NOTE_CHANGED
    return CF_NOTE_OTHER % CF_WHY_TEXT.get(why, "CurseForge could not be asked")


def _cf_project_block(rec):
    cf = rec["cf"]
    project = cf.get("project") or {}
    block = {"id": cf["installed"]["project_id"]}
    if isinstance(project.get("page_url"), str) and project["page_url"].startswith("https://"):
        block["page_url"] = project["page_url"]
    name = untrusted.clean_name(project.get("name") or "")
    if name:
        block["name"] = name
    slug = untrusted.clean(project.get("slug") or "", 60)
    if slug:
        block["slug"] = slug
    return block


# --------------------------------------------------------------------------- the check

def check_folder(folder=None, default_folder="~/Downloads", mc_version=None, loader=None, channel="release",
                 quilt_fallback=True, deadline_seconds=0):
    """Do the mods in one folder have newer compatible files?  See the module docstring.  Never raises."""
    try:
        return _check(folder, default_folder, mc_version, loader, channel, quilt_fallback, deadline_seconds)
    except Exception as exc:  # noqa: BLE001 - this never raises
        return fail(UNSUPPORTED, "The update check failed (%s). Nothing was changed." % type(exc).__name__)


def _needs_folder(default_folder):
    """No folder named and none checked before: ask, suggesting the default_dest setting.  Nothing is read or sent."""
    text = default_folder.strip() if isinstance(default_folder, str) and default_folder.strip() else "~/Downloads"
    path, _how, bad = resolve_folder(text, text, {})          # only looks whether the suggestion is a usable folder
    ask = "Which folder should I check? Give the full path of your mods folder, for example D:/Minecraft/mods"
    out = {"ok": True, "needs_folder": True, "needs_clarification": True, "nothing_changed": True, "checked": 0,
           "updates_available": 0, "updates": [], "up_to_date": [], "not_checked": []}
    if path is not None:
        ask += (", or say yes to use your default download folder, %s (the default_dest setting; a different one is "
                "chosen by naming it)." % path)
        out["suggested_folder"] = str(path)
        out["suggestion_from"] = "default_dest"
    else:
        ask += ". (Your default_dest setting, %s, can't be used: %s)" % (text[:80], str(bad.get("error") or "")[:120])
    out["ask"] = ask
    out["message"] = ask + " Nothing was read, hashed, sent or changed."
    return out


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
    if not (isinstance(folder, str) and folder.strip()) and not state.get("last_folder"):
        return _needs_folder(default_folder)                  # never silently hash ~/Downloads
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
        rec = {"name": item["name"], "sha1": item["sha1"], "size": item["size"], "status": None, "item": item}
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

    # The files Modrinth does not know, asked of CurseForge (its records are kept apart: no "installed" key, so a
    # CurseForge id is never sent to Modrinth).  A fingerprint computed now is remembered next to the SHA-1.
    cf = _curseforge_half(records, kept, mc_arg, loader_arg, channel, quilt_fallback, deadline, texts)
    if cf is not None and cf.pop("state_changed", False):
        save_state({"last_folder": str(path), "hashes": kept})
    if _late(deadline):
        scanned["partial"] = True
    return _assemble(out, records, names, texts, scanned, cf)


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


def _cf_entries(rec, shown):
    """The answer's entry for a file CurseForge decided -> (list name, entry)."""
    cf, installed = rec["cf"], rec["cf"]["installed"]
    block = _cf_project_block(rec)
    if rec["status"] == "update_available":
        latest = cf["latest"]
        entry = {"file": shown, "source": "curseforge", "matched_by": "fingerprint", "project": block,
                 "installed": {"version": untrusted.clean(installed.get("version_label") or "", 60),
                               "channel": str(installed.get("channel") or ""), "date": _day(installed.get("date"))},
                 "latest": {"version": untrusted.clean(latest.get("version_label") or "", 60), "file_id": latest["file_id"],
                            "channel": str(latest.get("channel") or ""), "date": _day(latest.get("date")),
                            "file_name": untrusted.clean_file_name(latest.get("file_name") or ""),
                            "size": latest.get("size") if isinstance(latest.get("size"), int) else None},
                 "get_with": {"tool": "download_mc_file", "source": "curseforge", "project": installed["project_id"],
                              "file": latest["file_id"]}}
        if rec.get("via"):
            entry["latest"]["via"] = rec["via"]
        return "updates", entry
    return "up_to_date", {"file": shown, "project": block.get("name") or installed["project_id"],
                          "version": untrusted.clean(installed.get("version_label") or "", 60), "source": "curseforge",
                          "matched_by": "fingerprint"}


def _assemble(out, records, names, texts, scanned, cf=None):
    updates, current, unchecked, other = [], [], [], []
    for rec in records:
        shown = untrusted.clean_file_name(rec["name"])
        if rec.get("cf") is not None and rec["status"] in ("update_available", "up_to_date"):
            where, entry = _cf_entries(rec, shown)
            (updates if where == "updates" else current).append(entry)
        elif rec["status"] == "update_available":
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
            other.append(dict({"file": shown, "why": rec.get("why"), "detail": rec.get("detail")}, **(rec.get("extra") or {})))
        else:
            entry = {"file": shown, "why": rec.get("why") or VENDOR_CHANGED, "detail": rec.get("detail") or ""}
            entry.update(rec.get("extra") or {})
            if rec.get("why") == NOT_DISTRIBUTABLE and rec.get("cf") is not None:
                latest = rec["cf"].get("latest") or {}
                entry["project"] = _cf_project_block(rec)
                entry["newer"] = {"version": untrusted.clean(latest.get("version_label") or "", 60),
                                  "date": _day(latest.get("date"))}
            unchecked.append(entry)

    out.update({"checked": len(updates) + len(current), "updates_available": len(updates), "updates": updates,
                "up_to_date": current, "not_checked": unchecked})
    if other:
        out["other_version"] = other
    if scanned.get("partial"):
        out["partial"] = True
    if cf is not None:
        # ONE hint about CurseForge, and the machine-readable state behind it (no key and no vendor answer in it)
        block = {k: cf[k] for k in ("status", "route", "verified", "asked", "matched", "why", "stopped_by") if k in cf}
        out["curseforge"] = block
        out["curseforge_note"] = _cf_note(cf)
        out["sources"] = ["modrinth", "curseforge"] if cf.get("status") == "checked" else ["modrinth"]
    ask = sorted({u["why"] for u in unchecked if u["why"] in ("needs_mc_version", "needs_loader")})
    if ask:
        out["needs_clarification"] = True
        out["ask"] = " and ".join({"needs_mc_version": "which Minecraft version you play",
                                   "needs_loader": "which loader you use (fabric, forge, neoforge or quilt)"}[a] for a in ask)
    via_cf = [e for e in updates + current if e.get("source") == "curseforge"]
    bits = ["%d of %d file(s) checked against %s have a newer compatible file" % (
                len(updates), len(updates) + len(current), "Modrinth and CurseForge" if via_cf else "Modrinth"),
            "%d are up to date" % len(current)]
    if unchecked:
        bits.append("%d could not be checked (reasons listed)" % len(unchecked))
    if other:
        bits.append("%d are built for another Minecraft version or loader" % len(other))
    message = "In %s: %s. " % (out["folder"], "; ".join(bits))
    if out.get("partial"):
        message += "The check stopped early (the time allowed ran out or the folder is very large), so the list is partial. "
    if via_cf:
        message += ("%d file(s) were matched on CurseForge by their fingerprint; that route is UNVERIFIED (never tried against "
                    "a real CurseForge file). " % len(via_cf))
    message += ("Nothing was downloaded, replaced, updated or installed. To get a newer file, ask for it with "
                "download_mc_file (it queues it in Downloads and leaves the old file alone).")
    out["message"] = message
    return untrusted.mark(out, *texts)
