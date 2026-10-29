"""Read an existing modpack, read only (Minecraft tools, agent M4).  actions/minecraft_packread.py is the tool.

WHAT THIS IS
  read_pack(path)   opens a Modrinth `.mrpack` (`modrinth.index.json`) or a CurseForge `.zip` (`manifest.json`) and
                    returns a plain structure: name, version, Minecraft version, loader and loader version, every file
                    the pack lists, how many override files it carries, and whatever looked wrong.  It never raises.
  check_path(raw)   the checks on a pack path the owner names (absolute, no '..', a plain file, not a link, a .mrpack or
                    .zip, and `outdir.protected_reason(path, writing=False)` as written AND as it really resolves).
  add_names(info)   optional: fills in project names.  The only function here that may touch the network, and only
                    through the adapters (modhosts/sources.py -> net.py): Modrinth gets SHA-1 hashes, CurseForge gets
                    project ids.  Nothing else of the pack ever leaves the PC.

WHAT IT NEVER DOES
  * It extracts nothing to disk.  The zip is opened and its central directory read; exactly ONE member (the index) is
    read, in memory, through a bounded reader.  The `overrides/` entries are LISTED (names and declared sizes), never
    read.  Nothing the pack contains is run, installed or followed: a `.mrpack` can list any download URL, and none is
    fetched.  Only the HOST of each link is reported, and a host that is not on Modrinth's list is a warning.
  * It never trusts a pack.  A zip is refused (reason says which rule) when it has too many entries, a declared size or
    compression ratio that looks like a zip bomb, an index over the size limit, an encrypted or exotic-compression entry,
    a name that is absolute, has a drive, a backslash, a colon, a control character or a `..` part, a duplicate name
    (compared case-insensitively), two entries that start at the same place in the file, or an entry that is a link.
    The limits are module constants below so a test can lower them.
  * It never guesses.  An unknown `formatVersion` / `manifestVersion` is REPORTED (reason unsupported, with the number),
    not read as if it were version 1.  A pack with both index files is refused as ambiguous.

STRANGER TEXT.  The pack's name, version, summary, author, file names, paths and every project name are written by
strangers: they are stripped of markup, clipped, and the result carries `untrusted_note` (modhosts/untrusted.mark).
`files[].path` of a Modrinth pack is where a launcher WRITES the file, so an unsafe one (absolute, `..`, a drive) is
flagged `unsafe_path` and listed as a problem.

Every function returns a plain dict and never raises.  Failure shape: modhosts.fail(reason, sentence, ...) plus
`refused` (a short machine word for the rule that stopped it).
"""
import json
import os
import re
import stat
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from . import (BAD_DEST, NOT_FOUND, OFFLINE, TOO_BIG, UNSUPPORTED, VERIFY_FAILED, fail, hosts, outdir, resolve,
               sources, untrusted)

INDEX_NAME = "modrinth.index.json"
MANIFEST_NAME = "manifest.json"
MRPACK_OVERRIDE_DIRS = ("overrides", "client-overrides", "server-overrides")
CURSEFORGE_DEFAULT_OVERRIDES = "overrides"
SUPPORTED_FORMAT_VERSION = 1
LOADER_KEYS = {"fabric-loader": "fabric", "quilt-loader": "quilt", "forge": "forge", "neoforge": "neoforge"}
SIDES = ("required", "optional", "unsupported")

# ---- hard limits.  Module constants (tests lower them); the defaults suit real packs, which can carry a few hundred MB
# of overrides but never a million entries or a ratio of hundreds to one.
MAX_PACK_BYTES = 4 * 1024 ** 3           # the .mrpack / .zip file itself
MAX_ENTRIES = 20000                      # zip members
MAX_TOTAL_BYTES = 16 * 1024 ** 3         # sum of declared uncompressed sizes
MAX_ENTRY_BYTES = 1024 ** 3              # one member, declared uncompressed
MAX_INDEX_BYTES = 8 * 1024 ** 2          # the one file that is read
MAX_RATIO = 200                          # uncompressed / compressed, one member
RATIO_FLOOR = 1024 ** 2                  # a member below this size may compress as well as it likes
MAX_TOTAL_RATIO = 200                    # sum of declared sizes / the pack file's own size
TOTAL_RATIO_FLOOR = 64 * 1024 ** 2
MAX_NAME_LEN = 400
MAX_INDEX_FILES = 20000                  # entries of the index's file list that are looked at
MAX_PROBLEMS = 50
MAX_SAMPLE = 20
MAX_NAMES = 2000                         # how many entries add_names() asks about

NAME_LEN, VERSION_LEN, AUTHOR_LEN, SUMMARY_LEN, PATH_LEN = 100, 40, 100, 256, 200
RESOLVE_HINT_NO_KEY = ("Project names for a CurseForge pack need a CurseForge key (the owner's own, set in Settings); "
                       "until then only the project and file ids are shown.")

_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA512_RE = re.compile(r"^[0-9a-fA-F]{128}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EXEC_EXT = (".exe", ".bat", ".cmd", ".ps1", ".sh", ".msi", ".dll", ".com", ".scr", ".vbs", ".lnk", ".jar")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


# --------------------------------------------------------------------------- small helpers

def _text(value, limit):
    return untrusted.clean_name("" if value is None else str(value), limit)


def _refuse(reason, why, error, name=None, **extra):
    """A refusal: ok False, the rule that stopped it in `refused`, and (when a stranger's name explains it) that name
    clipped and marked."""
    out = fail(reason, error, refused=why, **extra)
    if name is not None:
        out["entry"] = untrusted.clip(name, 60)
        untrusted.mark(out, name)
    return out


def member_problem(name):
    """Why a zip member name is not safe, as a short word, or None.  The same rules the pack writer applies when it
    re-checks a pack (modhosts/packs.py `_member_ok`), plus a length cap."""
    if not isinstance(name, str) or not name:
        return "empty_name"
    if len(name) > MAX_NAME_LEN:
        return "long_name"
    if "\\" in name:
        return "backslash"
    if "\x00" in name or _CTRL_RE.search(name):
        return "control_character"
    if name.startswith("/"):
        return "absolute_path"
    if _DRIVE_RE.match(name):
        return "drive_path"
    if ":" in name:
        return "colon"
    parts = name[:-1].split("/") if name.endswith("/") else name.split("/")
    for part in parts:
        if part == "":
            return "empty_part"
        if part == "..":
            return "parent_part"
        if part == ".":
            return "dot_part"
    return None


def _is_link(info):
    mode = info.external_attr >> 16
    if info.create_system == 3 and stat.S_ISLNK(mode):
        return True
    return bool(info.external_attr & _REPARSE)           # a Windows reparse point (junction / symlink)


def _problem(problems, where, what):
    if len(problems) < MAX_PROBLEMS:
        problems.append({"where": untrusted.clip(where, 80), "problem": untrusted.clip(what, 200)})
    else:
        problems.append(None)                              # counted, not listed; _finish() turns the tail into a count


def _finish_problems(problems):
    kept = [p for p in problems if p]
    more = len(problems) - len(kept)
    return kept, more


# --------------------------------------------------------------------------- the path the owner names

def check_path(raw):
    """The pack file the owner named -> (Path, None) | (None, failure).  Nothing is opened here."""
    if not isinstance(raw, str) or not raw.strip():
        return None, fail(BAD_DEST, "Give the full path of the pack file, like D:/Packs/MyPack.mrpack.")
    text = raw.strip()
    if "\x00" in text:
        return None, fail(BAD_DEST, "That file name has an invalid character.")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        return None, fail(BAD_DEST, "Give the full path of the pack file like D:/Packs/MyPack.mrpack; '%s' isn't one."
                          % text[:80])
    if ".." in path.parts or len(path.parts) <= 1:
        return None, fail(BAD_DEST, "'%s' isn't usable: no '..' and no drive root." % text[:80])
    if not path.name.lower().endswith((".mrpack", ".zip")):
        return None, fail(UNSUPPORTED, "That isn't a pack file: a Modrinth pack is a .mrpack and a CurseForge pack a .zip.")
    why = outdir.protected_reason(path, writing=False)
    if why:
        return None, fail(BAD_DEST, "Won't read that: %s." % why)
    for candidate in [path] + list(path.parents):
        if outdir.is_link(candidate):
            return None, fail(BAD_DEST, "Won't read %s: it is, or sits behind, a symlink or shortcut." % str(path)[:120])
    if not os.path.lexists(path):
        return None, fail(NOT_FOUND, "There is no file %s." % str(path)[:120])
    if not os.path.isfile(path):
        return None, fail(BAD_DEST, "%s isn't a plain file." % str(path)[:120])
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, fail(BAD_DEST, "%s can't be read." % str(path)[:120])
    if size > MAX_PACK_BYTES:
        return None, _refuse(TOO_BIG, "pack_too_big", "That file is larger than %d GB, more than a pack should be." %
                             (MAX_PACK_BYTES // 1024 ** 3))
    if size == 0:
        return None, fail(UNSUPPORTED, "That file is empty.")
    return path, None


# --------------------------------------------------------------------------- the zip, checked before anything is read

def _scan_zip(zf, pack_size):
    """Check every member's NAME and DECLARED numbers.  -> (infos, None) | (None, refusal).  Nothing is read."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        return None, _refuse(TOO_BIG, "too_many_entries", "The pack has more than %d entries; it is not read." % MAX_ENTRIES)
    seen, offsets, total = {}, {}, 0
    for info in infos:
        name = info.filename
        why = member_problem(name)
        if why:
            return None, _refuse(VERIFY_FAILED, why, "The pack has an entry with an unsafe name (%s); it is not read."
                                 % why.replace("_", " "), name)
        key = name.rstrip("/").lower()
        if key in seen:
            return None, _refuse(VERIFY_FAILED, "duplicate_name", "The pack has two entries with the same name; it is "
                                                                   "not read.", name)
        seen[key] = True
        if info.flag_bits & 0x1:
            return None, _refuse(VERIFY_FAILED, "encrypted", "The pack has an encrypted entry; it is not read.", name)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            return None, _refuse(UNSUPPORTED, "compression", "The pack uses a compression method packs never use; it is "
                                                              "not read.", name)
        if _is_link(info):
            return None, _refuse(VERIFY_FAILED, "link", "The pack has a link (symlink) entry; it is not read.", name)
        if info.file_size > MAX_ENTRY_BYTES:
            return None, _refuse(TOO_BIG, "entry_too_big", "The pack has an entry larger than %d MB; it is not read."
                                 % (MAX_ENTRY_BYTES // 1024 ** 2), name)
        if info.file_size > RATIO_FLOOR and info.file_size > MAX_RATIO * max(1, info.compress_size):
            return None, _refuse(TOO_BIG, "compression_ratio", "The pack has an entry that expands more than %d to 1 "
                                                               "(a zip bomb); it is not read." % MAX_RATIO, name)
        if info.header_offset in offsets and not name.endswith("/"):
            return None, _refuse(VERIFY_FAILED, "overlapping_entries", "The pack has entries that overlap inside the "
                                                                        "file; it is not read.", name)
        offsets[info.header_offset] = True
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            return None, _refuse(TOO_BIG, "total_too_big", "The pack would expand to more than %d GB; it is not read."
                                 % (MAX_TOTAL_BYTES // 1024 ** 3))
    if total > TOTAL_RATIO_FLOOR and total > MAX_TOTAL_RATIO * max(1, pack_size):
        return None, _refuse(TOO_BIG, "compression_ratio", "The pack expands more than %d to 1 as a whole (a zip bomb); "
                                                           "it is not read." % MAX_TOTAL_RATIO)
    return infos, None


def _read_member(zf, info):
    """The bytes of ONE member, through a bounded reader.  The declared size is never trusted: at most MAX_INDEX_BYTES+1
    bytes are asked for.  -> (bytes, None) | (None, refusal)."""
    if info.file_size > MAX_INDEX_BYTES:
        return None, _refuse(TOO_BIG, "index_too_big", "The pack's index file is larger than %d MB; it is not read."
                             % (MAX_INDEX_BYTES // 1024 ** 2))
    try:
        with zf.open(info) as handle:
            data = handle.read(MAX_INDEX_BYTES + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, ValueError, EOFError):
        return None, fail(UNSUPPORTED, "The pack's index file couldn't be read: the zip is damaged.", refused="damaged")
    if len(data) > MAX_INDEX_BYTES:
        return None, _refuse(TOO_BIG, "index_too_big", "The pack's index file is larger than %d MB; it is not read."
                             % (MAX_INDEX_BYTES // 1024 ** 2))
    return data, None


def _parse_json(data, label):
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        return None, fail(UNSUPPORTED, "The pack's %s doesn't parse as JSON, so it isn't read." % label, refused="bad_json")
    if not isinstance(doc, dict):
        return None, fail(UNSUPPORTED, "The pack's %s isn't a JSON object, so it isn't read." % label, refused="bad_json")
    return doc, None


# --------------------------------------------------------------------------- overrides: listed, never read

def _override_summary(infos, groups):
    """`groups` = the folder names that hold overrides.  -> (summary, entries) where entries are the FILE members
    [(name, group, rel, declared_size, crc)] and `others` the members that are neither the index nor an override."""
    folders = {g: {"files": 0, "bytes": 0} for g in groups}
    entries, others, sample, executable = [], [], [], 0
    for info in infos:
        name = info.filename
        if name.endswith("/") or name in (INDEX_NAME, MANIFEST_NAME):
            continue
        first, _slash, rest = name.partition("/")
        if first in folders and rest:
            folders[first]["files"] += 1
            folders[first]["bytes"] += info.file_size
            entries.append((name, first, rest, info.file_size, info.CRC))
            if rest.lower().endswith(_EXEC_EXT) and not rest.lower().startswith("mods/"):
                executable += 1
            if len(sample) < MAX_SAMPLE:
                sample.append(untrusted.clip(name, PATH_LEN))
        else:
            others.append(name)
    summary = {"folders": folders, "files": sum(f["files"] for f in folders.values()),
               "bytes": sum(f["bytes"] for f in folders.values()), "sample": sample,
               "never_read": True}
    if executable:
        summary["program_like_files"] = executable
    return summary, entries, others


# --------------------------------------------------------------------------- Modrinth

def _host_of(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, False
    host = (parts.hostname or "").lower()
    return host, parts.scheme == "https"


def _clean_rel_path(path):
    """True for a path a launcher may write below the instance folder (the same rule the pack writer applies)."""
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
        return False
    if _DRIVE_RE.match(path):
        return False
    return all(p not in ("", ".", "..") and not _CTRL_RE.search(p) and ":" not in p for p in path.split("/"))


def _mrpack_env(raw):
    if raw is None:
        return None, True
    if not isinstance(raw, dict):
        return None, False
    out = {}
    for side in ("client", "server"):
        value = raw.get(side)
        if value not in SIDES:
            return None, False
        out[side] = value
    return out, True


def _parse_modrinth(doc, infos, problems, internal):
    fv = doc.get("formatVersion")
    if fv != SUPPORTED_FORMAT_VERSION or isinstance(fv, bool):
        return fail(UNSUPPORTED, "This is a Modrinth pack of format version %s; Jarvis reads version %d only and doesn't "
                                 "guess at others." % (untrusted.clip(fv, 20), SUPPORTED_FORMAT_VERSION),
                    refused="format_version", format="modrinth", format_version=untrusted.clip(fv, 20))
    if doc.get("game") != "minecraft":
        return fail(UNSUPPORTED, "This pack is for a game that isn't Minecraft (%s)." % untrusted.clip(doc.get("game"), 30),
                    refused="game", format="modrinth")
    deps = doc.get("dependencies")
    mc = loader = loader_version = None
    extra_deps = []
    deps_ok = isinstance(deps, dict)
    if not deps_ok:
        _problem(problems, "dependencies", "missing: the Minecraft version and loader are unknown")
        deps = {}
    raw_mc = deps.get("minecraft")
    mc = resolve.normalise_mc_version(raw_mc) if isinstance(raw_mc, str) else None
    if mc is None:
        _problem(problems, "dependencies.minecraft", "missing or not a Minecraft version like 1.20.1")
    loaders_found = [(LOADER_KEYS[k], v) for k, v in deps.items() if k in LOADER_KEYS]
    for key in deps:
        if key != "minecraft" and key not in LOADER_KEYS:
            extra_deps.append(untrusted.clip(key, 40))
    if len(loaders_found) == 1 and isinstance(loaders_found[0][1], str) and loaders_found[0][1].strip():
        loader, loader_version = loaders_found[0][0], untrusted.clip(loaders_found[0][1], 40)
    elif len(loaders_found) > 1:
        _problem(problems, "dependencies", "more than one loader is listed, so none is picked")
    elif not loaders_found and deps_ok:
        loader = "vanilla"                                  # no loader dependency: a pack that needs no mod loader
    if extra_deps:
        _problem(problems, "dependencies", "unknown dependency keys that are not read: %s" % ", ".join(extra_deps[:5]))

    raw_files = doc.get("files")
    if not isinstance(raw_files, list):
        _problem(problems, "files", "the file list is missing or not a list")
        raw_files = []
    if len(raw_files) > MAX_INDEX_FILES:
        _problem(problems, "files", "more than %d files; the rest are not looked at" % MAX_INDEX_FILES)
        raw_files = raw_files[:MAX_INDEX_FILES]
    files, raw_kept, seen_paths = [], [], {}
    foreign, plain_http = {}, 0
    for index, item in enumerate(raw_files):
        label = "files[%d]" % index
        if not isinstance(item, dict):
            _problem(problems, label, "isn't an object")
            files.append({"path": "", "problems": ["not_an_object"]})
            raw_kept.append(None)
            continue
        flags = []
        path = item.get("path")
        path_ok = _clean_rel_path(path)
        if not path_ok:
            flags.append("unsafe_path")
            _problem(problems, label, "its path isn't safe to write below a game folder (absolute, '..' or a drive)")
        elif path.lower() in seen_paths:
            flags.append("duplicate_path")
            _problem(problems, label, "another file has the same path")
        if isinstance(path, str):
            seen_paths[path.lower()] = True
        hashes = item.get("hashes") if isinstance(item.get("hashes"), dict) else {}
        sha1 = str(hashes.get("sha1") or "")
        sha512 = str(hashes.get("sha512") or "")
        if not _SHA1_RE.match(sha1):
            flags.append("no_sha1")
            sha1 = ""
        if not _SHA512_RE.match(sha512):
            flags.append("no_sha512")
            sha512 = ""
        size = item.get("fileSize")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            flags.append("no_size")
            size = None
        env, env_ok = _mrpack_env(item.get("env"))
        if not env_ok:
            flags.append("bad_env")
        urls = [u for u in item.get("downloads") or [] if isinstance(u, str)] if isinstance(item.get("downloads"), list) else []
        link_hosts = []
        for url in urls[:10]:
            host, secure = _host_of(url)
            if not host:
                flags.append("bad_link")
                continue
            if not secure:
                plain_http += 1
                flags.append("insecure_link")
            if host not in link_hosts:
                link_hosts.append(untrusted.clip(host, 100))
            if host not in hosts.MODRINTH_HOSTS:
                foreign[host] = foreign.get(host, 0) + 1
        if not urls:
            flags.append("no_download_link")
        flags = sorted(set(flags))
        base = path.rsplit("/", 1)[-1] if isinstance(path, str) else ""
        view = {"path": untrusted.clip(path, PATH_LEN) if isinstance(path, str) else "",
                "file_name": untrusted.clean_file_name(base), "sha1": sha1.lower(), "sha512": sha512.lower(),
                "size": size, "hosts": link_hosts, "env": env}
        if flags:
            view["problems"] = flags
        files.append(view)
        raw_kept.append({"path": path if isinstance(path, str) else None, "sha1": sha1.lower(), "sha512": sha512.lower(),
                         "size": size, "urls": urls[:10], "env": env, "flags": flags})
    warnings = []
    if foreign:
        names = sorted(foreign)[:5]
        warnings.append("%d file link%s point to a host that isn't on Modrinth's list (%s). Jarvis fetches nothing from a "
                        "pack, and a launcher may follow those links." % (sum(foreign.values()),
                                                                          "" if sum(foreign.values()) == 1 else "s",
                                                                          ", ".join(untrusted.clip(n, 60) for n in names)))
    if plain_http:
        warnings.append("%d file link%s use plain http, not https." % (plain_http, "" if plain_http == 1 else "s"))
    summary, override_entries, others = _override_summary(infos, MRPACK_OVERRIDE_DIRS)
    internal.update({"files": raw_kept, "override_entries": override_entries, "others": others,
                     "extra_deps": extra_deps, "overrides_dir": None,
                     "extra_index_keys": sorted(k for k in doc if k not in (
                         "formatVersion", "game", "versionId", "name", "summary", "files", "dependencies"))})
    result = {"ok": True, "format": "modrinth", "format_version": SUPPORTED_FORMAT_VERSION,
              "name": _text(doc.get("name"), NAME_LEN), "version": _text(doc.get("versionId"), VERSION_LEN),
              "summary": _text(doc.get("summary"), SUMMARY_LEN), "mc_version": mc, "loader": loader,
              "loader_version": loader_version, "files": files, "overrides": summary, "warnings": warnings,
              "hosts_outside_modrinth": sorted(foreign)[:20]}
    result["_texts"] = [doc.get("name"), doc.get("versionId"), doc.get("summary")] + [f.get("path") for f in files]
    return result


# --------------------------------------------------------------------------- CurseForge

def _int_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _parse_curseforge(doc, infos, problems, internal):
    mv = doc.get("manifestVersion")
    if doc.get("manifestType") != "minecraftModpack":
        return fail(UNSUPPORTED, "This manifest.json isn't a Minecraft modpack manifest.", refused="manifest_type",
                    format="curseforge")
    if mv != SUPPORTED_FORMAT_VERSION or isinstance(mv, bool):
        return fail(UNSUPPORTED, "This is a CurseForge pack of manifest version %s; Jarvis reads version %d only and "
                                 "doesn't guess at others." % (untrusted.clip(mv, 20), SUPPORTED_FORMAT_VERSION),
                    refused="format_version", format="curseforge", format_version=untrusted.clip(mv, 20))
    block = doc.get("minecraft")
    mc = loader = loader_version = loader_id = None
    block_ok = isinstance(block, dict)
    if not block_ok:
        _problem(problems, "minecraft", "missing: the Minecraft version and loader are unknown")
        block = {}
    raw_mc = block.get("version")
    mc = resolve.normalise_mc_version(raw_mc) if isinstance(raw_mc, str) else None
    if mc is None:
        _problem(problems, "minecraft.version", "missing or not a Minecraft version like 1.20.1")
    mod_loaders = block.get("modLoaders") if isinstance(block.get("modLoaders"), list) else []
    ids = [m for m in mod_loaders if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip()]
    chosen = [m for m in ids if m.get("primary") is True] or (ids if len(ids) == 1 else [])
    if not ids and block_ok:
        loader = "vanilla"
    elif not ids:
        pass
    elif len(chosen) != 1:
        _problem(problems, "minecraft.modLoaders", "more than one loader and none is primary, so none is picked")
    else:
        loader_id = untrusted.clip(chosen[0]["id"], 60)
        prefix, dash, rest = chosen[0]["id"].strip().partition("-")
        loader = resolve.normalise_loader(prefix) if dash else None
        loader_version = untrusted.clip(rest, 40) if dash and rest else None
        if loader is None or not loader_version:
            loader, loader_version = None, None
            _problem(problems, "minecraft.modLoaders", "the loader id isn't one Jarvis knows (fabric, forge, neoforge, quilt)")
    overrides_dir = doc.get("overrides", CURSEFORGE_DEFAULT_OVERRIDES)
    if not isinstance(overrides_dir, str) or not outdir.safe_part(overrides_dir):
        _problem(problems, "overrides", "the overrides folder name isn't a plain folder name; no overrides are counted")
        overrides_dir = None
    raw_files = doc.get("files")
    if not isinstance(raw_files, list):
        _problem(problems, "files", "the file list is missing or not a list")
        raw_files = []
    if len(raw_files) > MAX_INDEX_FILES:
        _problem(problems, "files", "more than %d files; the rest are not looked at" % MAX_INDEX_FILES)
        raw_files = raw_files[:MAX_INDEX_FILES]
    files, raw_kept, seen = [], [], {}
    for index, item in enumerate(raw_files):
        label = "files[%d]" % index
        if not isinstance(item, dict):
            _problem(problems, label, "isn't an object")
            files.append({"project_id": None, "file_id": None, "required": None, "problems": ["not_an_object"]})
            raw_kept.append(None)
            continue
        flags = []
        pid, fid = _int_id(item.get("projectID")), _int_id(item.get("fileID"))
        if pid is None or fid is None:
            flags.append("bad_ids")
            _problem(problems, label, "projectID / fileID aren't positive numbers")
        elif (pid, fid) in seen:
            flags.append("duplicate")
            _problem(problems, label, "the same project and file are listed twice")
        else:
            seen[(pid, fid)] = True
        required = item.get("required", True)
        if not isinstance(required, bool):
            flags.append("bad_required")
            _problem(problems, label, "required isn't true or false")
            required = None
        view = {"project_id": pid, "file_id": fid, "required": required}
        if flags:
            view["problems"] = sorted(set(flags))
        files.append(view)
        raw_kept.append({"project_id": pid, "file_id": fid, "required": required, "flags": sorted(set(flags))})
    summary, override_entries, others = (_override_summary(infos, (overrides_dir,)) if overrides_dir
                                         else ({"folders": {}, "files": 0, "bytes": 0, "sample": [], "never_read": True},
                                               [], [i.filename for i in infos if not i.filename.endswith("/")
                                                    and i.filename not in (INDEX_NAME, MANIFEST_NAME)]))
    internal.update({"files": raw_kept, "override_entries": override_entries, "others": others,
                     "extra_deps": [], "overrides_dir": overrides_dir,
                     "extra_index_keys": sorted(k for k in doc if k not in (
                         "minecraft", "manifestType", "manifestVersion", "name", "version", "author", "files", "overrides"))})
    result = {"ok": True, "format": "curseforge", "format_version": SUPPORTED_FORMAT_VERSION,
              "name": _text(doc.get("name"), NAME_LEN), "version": _text(doc.get("version"), VERSION_LEN),
              "author": _text(doc.get("author"), AUTHOR_LEN), "mc_version": mc, "loader": loader,
              "loader_version": loader_version, "loader_id": loader_id, "files": files, "overrides": summary,
              "warnings": []}
    result["_texts"] = [doc.get("name"), doc.get("version"), doc.get("author")]
    return result


# --------------------------------------------------------------------------- the one entry point

def read_pack(path, internal=False):
    """Read the pack at `path` (a Path or a string that has passed check_path).  Never raises.

    -> the structure described in the module docstring | a failure.  With internal=True the result also carries
    `_internal` (the raw index entries and the override member list, for packupdate.py): never given to the model."""
    try:
        return _read(Path(path), internal)
    except Exception as exc:  # noqa: BLE001 - nothing here raises
        return fail(UNSUPPORTED, "The pack couldn't be read (%s). Nothing was changed." % type(exc).__name__, refused="error")


def _read(path, internal):
    try:
        pack_size = os.path.getsize(path)
    except OSError:
        return fail(BAD_DEST, "%s can't be read." % str(path)[:120])
    try:
        zf = zipfile.ZipFile(str(path))
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        return fail(UNSUPPORTED, "That file isn't a readable zip, so it isn't a pack.", refused="not_a_zip")
    with zf:
        infos, bad = _scan_zip(zf, pack_size)
        if bad:
            return bad
        names = {i.filename: i for i in infos}
        has_index, has_manifest = INDEX_NAME in names, MANIFEST_NAME in names
        if has_index and has_manifest:
            return _refuse(UNSUPPORTED, "two_indexes", "The pack has both %s and %s; Jarvis won't guess which one is the "
                           "real one." % (INDEX_NAME, MANIFEST_NAME))
        if not has_index and not has_manifest:
            return fail(UNSUPPORTED, "There is no %s or %s at the top of that zip, so it isn't a modpack file."
                        % (INDEX_NAME, MANIFEST_NAME), refused="no_index")
        label, name = ("modrinth.index.json", INDEX_NAME) if has_index else ("manifest.json", MANIFEST_NAME)
        data, bad = _read_member(zf, names[name])
        if bad:
            return bad
        doc, bad = _parse_json(data, label)
        if bad:
            return bad
        problems, inner = [], {}
        result = (_parse_modrinth if has_index else _parse_curseforge)(doc, infos, problems, inner)
        if not result.get("ok"):
            return result
        kept, more = _finish_problems(problems)
        texts = result.pop("_texts", [])
        result["counts"] = {"files": len(result["files"]), "override_files": result["overrides"]["files"],
                            "other_entries": len(inner["others"])}
        if inner["others"]:
            result["other_entries_sample"] = [untrusted.clip(n, PATH_LEN) for n in inner["others"][:5]]
        result["problems"] = kept
        if more:
            result["problems_more"] = more
        result["complete"] = not kept and not more
        result["pack_bytes"] = pack_size
        result["resolved"] = False
        if internal:
            result["_internal"] = inner
        return untrusted.mark(result, *texts)


# --------------------------------------------------------------------------- names (the only network use)

def _modrinth_names(info, files, limit):
    """SHA-1 hashes go to Modrinth (POST /version_files) through the adapter; project ids come back, then their names.
    -> (named, note | None, failure | None)."""
    adapter = sources.get("modrinth")
    if adapter is None or not hasattr(adapter, "versions_by_hashes"):
        return 0, "Modrinth support isn't installed here, so project names weren't looked up.", None
    wanted = [f["sha1"] for f in files if f.get("sha1")][:limit]
    if not wanted:
        return 0, None, None
    got = adapter.versions_by_hashes(wanted, "sha1")
    if not got.get("ok"):
        return 0, None, got
    by_hash = got.get("files") or {}
    pids = sorted({str(f.get("project_id")) for f in by_hash.values() if isinstance(f, dict) and f.get("project_id")})
    projects = {}
    if pids:
        named = adapter.bulk_projects(pids)
        if not named.get("ok"):
            return 0, None, named
        projects = {k: v for k, v in (named.get("projects") or {}).items() if v}
    count = 0
    for view in files:
        found = by_hash.get(view.get("sha1")) if view.get("sha1") else None
        if not isinstance(found, dict):
            continue
        project = projects.get(str(found.get("project_id")))
        view["project_id"] = str(found.get("project_id") or "")
        view["file_id"] = str(found.get("file_id") or "")
        view["version"] = untrusted.clean_name(found.get("version_label") or "", 60)
        if project:
            view["project"] = untrusted.clean_name(project.get("name"), NAME_LEN)
            view["slug"] = project.get("slug") or ""
            if project.get("page_url"):
                view["page_url"] = project["page_url"]
            count += 1
    return count, None, None


def _curseforge_names(info, files, limit):
    if not sources.has_curseforge_key():
        return 0, RESOLVE_HINT_NO_KEY, None
    adapter = sources.get("curseforge")
    if adapter is None:
        return 0, "CurseForge support isn't installed here, so project names weren't looked up.", None
    ids = sorted({f["project_id"] for f in files if isinstance(f.get("project_id"), int)})[:limit]
    if not ids:
        return 0, None, None
    got = adapter.bulk_projects(ids)
    if not got.get("ok"):
        return 0, None, got
    projects = got.get("projects") or {}
    count = 0
    for view in files:
        project = projects.get(view.get("project_id"))
        if isinstance(project, dict):
            view["project"] = untrusted.clean_name(project.get("name"), NAME_LEN)
            view["slug"] = project.get("slug") or ""
            if project.get("page_url"):
                view["page_url"] = project["page_url"]
            count += 1
    return count, None, None


def add_names(info, enabled=True):
    """Resolve project names into `info["files"]` (in place).  Skipped entirely, with no network call, when `enabled` is
    False.  A failure (no key, offline, rate limit) is reported in `resolve_note`, never raised, and the pack structure
    stays whole."""
    try:
        if not isinstance(info, dict) or not info.get("ok"):
            return info
        if not enabled:
            info["resolved"] = False
            info["resolve_note"] = "Names were not looked up (resolve was off); nothing was sent anywhere."
            return info
        files = info.get("files") or []
        if info["format"] == "modrinth":
            count, note, bad = _modrinth_names(info, files, MAX_NAMES)
            sent = "SHA-1 hashes of the pack's files"
        else:
            count, note, bad = _curseforge_names(info, files, MAX_NAMES)
            sent = "project ids"
        if bad:
            reason = bad.get("reason")
            info["resolve_failed"] = reason if reason else "vendor_changed"
            info["resolve_note"] = ("Project names couldn't be looked up (%s); the pack was read all the same."
                                    % untrusted.clip(bad.get("error") or "the site didn't answer", 140))
            return info
        info["resolved"] = count > 0
        info["named"] = count
        if note:
            info["resolve_note"] = note
        elif count < len([f for f in files if f.get("sha1") or isinstance(f.get("project_id"), int)]):
            info["resolve_note"] = ("%d of %d entries were found on the site; the rest keep their file names and hashes "
                                    "(never guessed from a name). Sent: only %s." %
                                    (count, len(files), sent))
        else:
            info["resolve_note"] = "Project names were looked up on the site. Sent: only %s." % sent
        texts = [info.get("name"), info.get("summary"), info.get("version")]
        for view in files:
            texts += [view.get("project"), view.get("version"), view.get("path")]
        return untrusted.mark(info, *texts)
    except Exception as exc:  # noqa: BLE001
        if isinstance(info, dict):
            info["resolve_failed"] = OFFLINE
            info["resolve_note"] = "Project names couldn't be looked up (%s)." % type(exc).__name__
        return info
