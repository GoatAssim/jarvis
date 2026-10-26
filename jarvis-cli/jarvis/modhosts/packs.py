"""Modpack files: resolve a list of projects on ONE platform and write a Modrinth `.mrpack` or a CurseForge `.zip`.

(Minecraft tools, agent P5.  actions/minecraft_packs.py is the tool; this file is everything it needs.)

WHAT IT DOES
  plan_pack(args)   validates everything first (cheap, no network), reads the optional `src` folder for overrides,
                    works out the loader version, then resolves the entries in bulk on the pack's platform and adds the
                    required dependencies.  It never writes.  The answer is {"ok": True, "plan": plan} or a final result
                    (a failure, a clarifying question, or a partial result when the time limit was hit).
  describe(plan)    the owner-facing report of a plan (resolved / dependencies_added / skipped / overrides / warnings).
  write_pack(plan)  builds the zip in a temp file in the destination folder, re-opens it and re-parses its manifest
                    (check_pack), then moves it into place (outdir.write_atomic).  Nothing half-written is left behind.
  check_pack(path)  what a pack must satisfy to be accepted again: a zip that opens, no unsafe entry name, no duplicate,
                    a manifest that parses and is valid for its format.  Also used on the file just written.

ONE PLATFORM PER PACK.  `format=modrinth` resolves every entry on Modrinth, `format=curseforge` on CurseForge: the two
formats cannot reference each other's files.  An entry not found on that platform is REPORTED and SKIPPED, never swapped
for a lookalike.  A name that matches several projects is never guessed (resolve.resolve_project): the candidates are
listed in the report.  Nothing is installed: a pack is a file.

THE RULES
  * Modrinth entries need BOTH sha1 and sha512, an https link on cdn.modrinth.com and a size, or they are refused (skipped
    and reported); the writer refuses them again.  `path` is `mods/<sanitised name>` (resourcepacks/, shaderpacks/,
    datapacks/ for those kinds), never with '..', a drive or a leading slash.
  * A CurseForge file without a download link (the author blocks third-party distribution) is reported with its project
    page and left out of the manifest.  (Whether a manifest MAY list such a file is unverified; leaving it out is the safe
    default.)
  * Zip entry names are built by code from sanitised parts with forward slashes; nothing from an API answer becomes an
    entry name unsanitised.  Entries are sorted and carry a fixed timestamp, so the output is deterministic.
  * Overrides (`src`): an existing folder that is not protected (outdir.protected_reason: drive roots, the home folder
    itself, OS folders, ~/.jarvis) and does not CONTAIN ~/.jarvis, is not a link and has no link in its path, and does not
    hold the output folder.  Links inside are skipped and reported, never followed.  Hidden files and secrets (.git,
    .env, *.key, *.pem, ...) are skipped and reported, never packed.  A file is read through a handle that refuses links
    and is compared with what the scan saw.  The caps (file count, total size, one file's size) are tool settings, 0 =
    unlimited; a folder over a cap is refused.
  * The three text fields are clipped and stripped of control characters.  The loader version is `latest` (looked up by
    modhosts/loaders.py when it exists) or `given` (shape-checked, used exactly); a pack without one is refused with reason
    no_loader_version.
"""
import fnmatch
import json
import os
import re
import stat
import time
import zipfile

from . import (AMBIGUOUS, BAD_DEST, NO_COMPATIBLE_FILE, NO_LOADER_VERSION, NOT_DISTRIBUTABLE, NOT_FOUND, OFFLINE,
               TOO_BIG, UNSUPPORTED, VENDOR_CHANGED, VERIFY_FAILED, fail, outdir, resolve, sources, untrusted)

FORMATS = ("modrinth", "curseforge")
EXTENSION = {"modrinth": ".mrpack", "curseforge": ".zip"}
INDEX_NAME = "modrinth.index.json"
MANIFEST_NAME = "manifest.json"
OVERRIDES = "overrides"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
MRPACK_DOWNLOAD_HOSTS = ("cdn.modrinth.com",)
SIDES = ("required", "optional", "unsupported")
CHANNELS = ("release", "beta", "alpha")

FOLDER_OF_KIND = {"mod": "mods", "resourcepack": "resourcepacks", "shader": "shaderpacks", "datapack": "datapacks"}
EXTENSIONS_OF_KIND = {"mod": (".jar",), "resourcepack": (".zip",), "shader": (".zip",), "datapack": (".zip",)}

# Defensive parsing limits: they protect memory, they are not the owner's caps (those are tool settings, 0 = unlimited).
DEFENSIVE_ENTRIES = 5000
DEFENSIVE_WALK = 200000
DEFENSIVE_MANIFEST_BYTES = 64 * 1024 * 1024
REPORT_SKIPPED_MAX = 50

NAME_LEN, VERSION_LEN, AUTHOR_LEN, SUMMARY_LEN = 100, 40, 100, 256
FALLBACK_LOADER_KEYS = {"fabric": "fabric-loader", "quilt": "quilt-loader", "forge": "forge", "neoforge": "neoforge"}
DEPENDENCY_KEYS = ("fabric-loader", "quilt-loader", "forge", "neoforge")
VANILLA = "vanilla"

# Names that are never packed into overrides (matched on the lower-case file name).
SECRET_PATTERNS = ("*.key", "*.pem", "*.pfx", "*.p12", "*.ppk", "*.jks", "*.keystore", "*.kdbx", "*.env", ".env*",
                   "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "credentials*", "secrets.*", "secret.*",
                   "*.token", "token.json", "tokens.json", "launcher_accounts*.json", "launcher_msa_credentials*.json",
                   "launcher_profiles.json", "microsoft_account*.json", "*.sqlite-wal")

_SHA1_RE = re.compile(r"[0-9a-fA-F]{40}")
_SHA512_RE = re.compile(r"[0-9a-fA-F]{128}")
_FILE_ID_RE = re.compile(r"[A-Za-z0-9._~-]{1,40}")
_LOADER_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,39}")
_DRIVE_RE = re.compile(r"[A-Za-z]:")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

CLOCK = time.monotonic            # tests replace it to hit the time limit without waiting


class _Bad(Exception):
    """A single entry cannot go into the pack: `code` says why (a short word), `text` is the sentence."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code, self.text = code, text


class _TimeUp(Exception):
    pass


# --------------------------------------------------------------------------- settings

def _setting(key, default=0):
    try:
        from .. import tool_settings
        return tool_settings.get("minecraft_lookup", key, default)
    except Exception:  # noqa: BLE001
        return default


def _cap(key, scale=1):
    """A cap setting (0 or unreadable = no cap) -> an int number of units, or None."""
    try:
        value = float(_setting(key, 0) or 0)
    except (TypeError, ValueError):
        return None
    return int(value * scale) if value > 0 else None


def _default_dest():
    raw = _setting("default_dest", "~/Downloads")
    return raw.strip() if isinstance(raw, str) and raw.strip() else "~/Downloads"


def _quilt_fallback():
    return bool(_setting("quilt_fabric_fallback", True))


def _flag(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _clarify(question):
    return {"ok": True, "needs_clarification": True, "question": question, "message": question}


def _text(value, limit):
    return untrusted.clip("" if value is None else str(value), limit)


# --------------------------------------------------------------------------- the entries

def _entry_failure(index, text):
    return fail(UNSUPPORTED, "Entry %d: %s" % (index + 1, text))


def parse_entries(raw):
    """The `entries` argument -> ([{"ref", "file", "required", "env", "source", "channel"}], None) | (None, result).
    Each item is a project name / id / slug, or an object {project, file?, required?, env?, source?, channel?}."""
    if raw is None or (isinstance(raw, (list, tuple)) and not raw):
        return None, _clarify("Which projects (mods, shaders, resource packs) should go into the pack? Give names, "
                              "slugs or ids.")
    if not isinstance(raw, (list, tuple)):
        return None, fail(UNSUPPORTED, "entries must be a list of project names, slugs or ids.")
    if len(raw) > DEFENSIVE_ENTRIES:
        return None, fail(TOO_BIG, "That is more than %d entries; split the pack." % DEFENSIVE_ENTRIES)
    out = []
    for i, item in enumerate(raw):
        if isinstance(item, bool) or item is None:
            return None, _entry_failure(i, "isn't a project name, slug or id.")
        entry = item if isinstance(item, dict) else {"project": item}
        if isinstance(item, (list, tuple, set)):
            return None, _entry_failure(i, "isn't a project name, slug or id.")
        ref = entry.get("project")
        ref = str(ref).strip() if isinstance(ref, (str, int)) and not isinstance(ref, bool) else ""
        if not ref or len(ref) > 100 or _CTRL_RE.search(ref):
            return None, _entry_failure(i, "needs a project name, slug or id (up to 100 characters).")
        parsed = {"ref": ref, "file": None, "required": None, "env": None, "source": None, "channel": None}
        if entry.get("file") not in (None, ""):
            fid = str(entry["file"]).strip()
            if not _FILE_ID_RE.fullmatch(fid):
                return None, _entry_failure(i, "file '%s' isn't a file id." % fid[:30])
            parsed["file"] = fid
        if entry.get("required") is not None:
            parsed["required"] = _flag(entry["required"], True)
        if entry.get("env") is not None:
            env = entry["env"]
            if not isinstance(env, dict) or any(k not in ("client", "server") for k in env) \
                    or any(v not in SIDES for v in env.values()):
                return None, _entry_failure(i, "env must look like {\"client\": \"required\", \"server\": \"optional\"} "
                                               "(required, optional or unsupported).")
            parsed["env"] = dict(env)
        if entry.get("source") not in (None, ""):
            src = str(entry["source"]).strip().lower()
            if src not in FORMATS:
                return None, _entry_failure(i, "source must be modrinth or curseforge.")
            parsed["source"] = src
        if entry.get("channel") not in (None, ""):
            chan = str(entry["channel"]).strip().lower()
            if chan not in CHANNELS:
                return None, _entry_failure(i, "channel must be release, beta or alpha.")
            parsed["channel"] = chan
        out.append(parsed)
    return out, None


# --------------------------------------------------------------------------- the loader version

def _loaders_module():
    try:
        from . import loaders
        return loaders
    except Exception:  # noqa: BLE001 - the resolver may not be merged yet: behave as if loader_version were required
        return None


def valid_loader_version(text):
    """Letters, digits, dots, dashes, 40 characters at most.  loaders.valid_loader_version too, when it exists."""
    if not isinstance(text, str) or not _LOADER_VERSION_RE.fullmatch(text):
        return False
    mod = _loaders_module()
    check = getattr(mod, "valid_loader_version", None)
    if callable(check):
        try:
            return bool(check(text))
        except Exception:  # noqa: BLE001
            return True
    return True


def mrpack_dependency_key(loader):
    mod = _loaders_module()
    fn = getattr(mod, "mrpack_dependency_key", None)
    if callable(fn):
        try:
            value = fn(loader)
            if isinstance(value, str) and value in DEPENDENCY_KEYS:
                return value
        except Exception:  # noqa: BLE001
            pass
    return FALLBACK_LOADER_KEYS.get(loader)


def curseforge_loader_id(loader, loader_version):
    mod = _loaders_module()
    fn = getattr(mod, "curseforge_loader_id", None)
    if callable(fn):
        try:
            value = fn(loader, loader_version)
            if isinstance(value, str) and value:
                return value
        except Exception:  # noqa: BLE001
            pass
    return "%s-%s" % (loader, loader_version)


def work_out_loader_version(loader, mc_version, given):
    """-> ({"value", "source": "given" | "latest", "resolved_as"?, "note"?}, None) | (None, failure)."""
    if given not in (None, ""):
        text = given.strip() if isinstance(given, str) else str(given)
        if not valid_loader_version(text):
            return None, fail(NO_LOADER_VERSION, "'%s' isn't a loader version (letters, digits, dots and dashes, 40 "
                                                 "characters at most)." % text[:50])
        return {"value": text, "source": "given"}, None
    mod = _loaders_module()
    fn = getattr(mod, "latest_loader_version", None)
    if not callable(fn):
        return None, fail(NO_LOADER_VERSION, "Say which %s version the pack should use (loader_version): looking up the "
                                             "latest one isn't available here." % loader)
    try:
        got = fn(loader, mc_version)
    except Exception:  # noqa: BLE001
        got = None
    if isinstance(got, dict) and got.get("ok") and valid_loader_version(str(got.get("loader_version") or "")):
        out = {"value": str(got["loader_version"]), "source": "latest"}
        if got.get("how") in ("recommended", "stable", "latest"):
            out["resolved_as"] = got["how"]
        if isinstance(got.get("note"), str) and got["note"]:
            out["note"] = untrusted.clip(got["note"], 200)
        return out, None
    cause = got.get("reason") if isinstance(got, dict) else None
    why = untrusted.clip(got.get("error"), 160) if isinstance(got, dict) and got.get("error") else ""
    return None, fail(NO_LOADER_VERSION, "Couldn't work out the latest %s version for Minecraft %s%s. Give loader_version "
                                         "yourself." % (loader, mc_version, " (%s)" % why if why else ""),
                      **({"cause": cause} if cause else {}))


# --------------------------------------------------------------------------- overrides (`src`)

def _contains_link(path):
    for candidate in [path] + list(path.parents):
        if outdir.is_link(candidate):
            return True
    return False


def _attrs(st):
    return getattr(st, "st_file_attributes", 0) or 0


def _hidden_reason(name, st):
    if name.startswith("."):
        return "hidden"
    if _attrs(st) & (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)):
        return "hidden"
    return None


def _secret_reason(name):
    low = name.lower()
    for pattern in SECRET_PATTERNS:
        if fnmatch.fnmatch(low, pattern):
            return "secret"
    return None


def scan_overrides(raw_src, dest_folder=None):
    """Read `src` WITHOUT following links and without opening any file.
    -> ({"folder": Path, "files": [(rel, abs, size)], "skipped": [{"path", "reason"}], "bytes": int,
         "skipped_count": int}, None) | (None, failure)."""
    from pathlib import Path
    text = str(raw_src).strip()
    if "\x00" in text:
        return None, fail(BAD_DEST, "That src folder name has an invalid character.")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        return None, fail(BAD_DEST, "src must be a full folder path like D:/Minecraft/config-for-pack; '%s' isn't one."
                          % text[:80])
    if ".." in path.parts or len(path.parts) <= 1:
        return None, fail(BAD_DEST, "'%s' isn't usable as src: no '..' and no drive root." % text[:80])
    why = outdir.protected_reason(path, writing=True)
    if why:
        return None, fail(BAD_DEST, "Won't pack that folder: %s." % why)
    if outdir.inside(Path.home() / ".jarvis", path):
        return None, fail(BAD_DEST, "Won't pack that folder: it contains Jarvis's own settings folder (~/.jarvis).")
    if _contains_link(path):
        return None, fail(BAD_DEST, "Won't pack %s: it is, or sits behind, a symlink or shortcut." % str(path)[:120])
    if not os.path.isdir(path):
        return None, fail(BAD_DEST, "src %s isn't an existing folder." % str(path)[:120])
    if dest_folder is not None and outdir.inside(dest_folder, path):
        return None, fail(BAD_DEST, "The output folder is inside src: the pack would pack itself. Choose a dest outside "
                                    "%s." % str(path)[:100])

    max_files = _cap("overrides_max_files")
    max_total = _cap("overrides_max_total_mb", 1024 * 1024)
    max_file = _cap("overrides_max_file_mb", 1024 * 1024)
    files, skipped, total, visited = [], [], 0, 0
    stack = [(str(path), "")]
    while stack:
        current, rel = stack.pop()
        try:
            with os.scandir(current) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError:
            skipped.append({"path": rel or ".", "reason": "unreadable"})
            continue
        for child in children:
            visited += 1
            if visited > DEFENSIVE_WALK:
                return None, fail(TOO_BIG, "src holds more than %d entries; pick a smaller folder." % DEFENSIVE_WALK)
            name = child.name
            rel_path = rel + "/" + name if rel else name
            try:
                st = child.stat(follow_symlinks=False)
            except OSError:
                skipped.append({"path": rel_path, "reason": "unreadable"})
                continue
            if stat.S_ISLNK(st.st_mode) or _attrs(st) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                skipped.append({"path": rel_path, "reason": "link"})
                continue
            hidden = _hidden_reason(name, st)
            if hidden:
                skipped.append({"path": rel_path, "reason": hidden})
                continue
            if stat.S_ISDIR(st.st_mode):
                if not outdir.safe_part(name):
                    skipped.append({"path": rel_path, "reason": "unsafe_name"})
                else:
                    stack.append((child.path, rel_path))
                continue
            if not stat.S_ISREG(st.st_mode):
                skipped.append({"path": rel_path, "reason": "not_a_file"})
                continue
            if _secret_reason(name):
                skipped.append({"path": rel_path, "reason": "secret"})
                continue
            if not outdir.safe_part(name):
                skipped.append({"path": rel_path, "reason": "unsafe_name"})
                continue
            size = int(st.st_size)
            if max_file is not None and size > max_file:
                return None, fail(TOO_BIG, "%s is larger than the one-file limit for overrides (%d MB, a tool setting)."
                                  % (rel_path[:100], max_file // (1024 * 1024)))
            total += size
            if max_total is not None and total > max_total:
                return None, fail(TOO_BIG, "src is larger than the overrides size limit (%d MB, a tool setting)."
                                  % (max_total // (1024 * 1024)))
            files.append((rel_path, child.path, size))
            if max_files is not None and len(files) > max_files:
                return None, fail(TOO_BIG, "src holds more than %d files (the overrides file limit, a tool setting)."
                                  % max_files)
    files.sort(key=lambda t: (t[0].lower(), t[0]))
    skipped.sort(key=lambda s: s["path"])
    return {"folder": path, "files": files, "skipped": skipped, "bytes": total, "skipped_count": len(skipped)}, None


def _open_for_read(abs_path, expected_size):
    """Open one scanned file for reading, refusing a link and a file that changed since the scan."""
    before = os.lstat(abs_path)
    if stat.S_ISLNK(before.st_mode) or _attrs(before) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise outdir.Refused(VERIFY_FAILED, "A file in src turned into a link while the pack was built; nothing was kept.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(abs_path, flags)
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or after.st_size != expected_size or not os.path.samestat(before, after):
            raise outdir.Refused(VERIFY_FAILED, "A file in src changed while the pack was built; nothing was kept.")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


# --------------------------------------------------------------------------- vetting one file for a pack

def _clean_file_name(file, kind):
    from ..hub import naming
    raw = str(file.get("file_name") or "")
    name = naming.sanitize_filename(raw, default="")
    if not name or not outdir.safe_part(name):
        raise _Bad("unsafe_file_name", "its file name isn't safe to put in a pack")
    if not name.lower().endswith(EXTENSIONS_OF_KIND.get(kind, (".jar",))):
        raise _Bad("unsafe_file_name", "its file isn't a %s" % "/".join(EXTENSIONS_OF_KIND.get(kind, (".jar",))))
    return name


def _rel_path_ok(path):
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
        return False
    if _DRIVE_RE.match(path):
        return False
    return all(p not in ("", ".", "..") and not _CTRL_RE.search(p) and ":" not in p for p in path.split("/"))


def _mrpack_entry(pf):
    """One `files[]` item of modrinth.index.json for a vetted pack file, or _Bad."""
    hashes = pf.get("hashes") or {}
    sha1, sha512 = str(hashes.get("sha1") or ""), str(hashes.get("sha512") or "")
    if not (_SHA1_RE.fullmatch(sha1) and _SHA512_RE.fullmatch(sha512)):
        raise _Bad("missing_hashes", "it has no sha1 and sha512 hashes, and a .mrpack needs both")
    url = pf.get("download_url")
    if not isinstance(url, str) or not _https_on(url, MRPACK_DOWNLOAD_HOSTS):
        raise _Bad("bad_download_url", "its download link isn't an https link on cdn.modrinth.com")
    size = pf.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise _Bad("missing_size", "its size isn't known, and a .mrpack needs it")
    path = pf.get("path")
    if not _rel_path_ok(path):
        raise _Bad("unsafe_file_name", "its pack path isn't safe")
    out = {"path": path, "hashes": {"sha1": sha1.lower(), "sha512": sha512.lower()}, "downloads": [url], "fileSize": size}
    if pf.get("env"):
        out["env"] = {"client": pf["env"]["client"], "server": pf["env"]["server"]}
    return out


def _https_on(url, hosts):
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and (parts.hostname or "").lower() in hosts and not parts.username \
            and not _CTRL_RE.search(url)
    except ValueError:
        return False


def _curseforge_entry(pf):
    try:
        pid, fid = int(pf["project_id"]), int(pf["file_id"])
    except (KeyError, TypeError, ValueError):
        raise _Bad("vendor_changed", "its ids aren't numbers") from None
    if pid <= 0 or fid <= 0:
        raise _Bad("vendor_changed", "its ids aren't valid")
    return {"projectID": pid, "fileID": fid, "required": bool(pf.get("required", True))}


def _vet(platform, file, project, entry, kind, dependency_of=None):
    """A file the resolver picked -> the pack-file dict, or _Bad."""
    if kind == "modpack":
        raise _Bad("unsupported_kind", "a modpack can't go inside a modpack")
    if platform == "curseforge":
        if project and project.get("distributable") is False or not file.get("downloadable"):
            raise _Bad(NOT_DISTRIBUTABLE, "the author doesn't allow third-party downloads of it, so it can't be listed "
                                          "for the CurseForge app to fetch")
    elif not file.get("downloadable"):
        raise _Bad("bad_download_url", "it has no download link")
    pf = {"project_id": str(file.get("project_id") or ""), "file_id": str(file.get("file_id") or ""),
          "kind": kind, "name": (project or {}).get("name") or str(file.get("project_id") or ""),
          "slug": (project or {}).get("slug") or "", "file_name": str(file.get("file_name") or ""),
          "version": file.get("version_label") or "", "channel": file.get("channel") or "release",
          "mc_versions": list(file.get("mc_versions") or [])[:6], "loaders": list(file.get("loaders") or [])[:5],
          "download_url": file.get("download_url"), "hashes": dict(file.get("hashes") or {}),
          "size": file.get("size"), "page_url": (project or {}).get("page_url") or ""}
    pf["incompatible"] = sorted({str(d.get("project_id")) for d in file.get("dependencies") or []
                                 if isinstance(d, dict) and d.get("type") == "incompatible" and d.get("project_id")})
    if file.get("via"):
        pf["via"] = file["via"]
    if dependency_of:
        pf["dependency_of"] = dependency_of
    if platform == "modrinth":
        pf["path"] = FOLDER_OF_KIND.get(kind, "mods") + "/" + _clean_file_name(file, kind)
        pf["env"] = _env_for(entry, project)
        _mrpack_entry(pf)                               # raises _Bad now, so the report says so
    else:
        pf["required"] = False if (entry or {}).get("required") is False else True
        _curseforge_entry(pf)
    return pf


def _env_for(entry, project):
    project = project or {}
    explicit = (entry or {}).get("env") or {}
    out = {}
    for side, key in (("client", "client_side"), ("server", "server_side")):
        value = explicit.get(side) or project.get(key)
        if (entry or {}).get("required") is False and value == "required" and side not in explicit:
            value = "optional"
        out[side] = value
    return out if all(v in SIDES for v in out.values()) else None      # unknown sides: left out, the launcher decides


# --------------------------------------------------------------------------- the manifests

def _json_bytes(doc):
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def build_manifest(plan):
    """The manifest document of a plan -> (file name in the zip, dict).  Raises outdir.Refused."""
    files = plan["files"]
    try:
        if plan["format"] == "modrinth":
            entries = sorted((_mrpack_entry(pf) for pf in files), key=lambda e: (e["path"].lower(), e["path"]))
            names = [e["path"].lower() for e in entries]
            if len(set(names)) != len(names):
                raise _Bad("name_clash", "two files would have the same path")
            deps = {"minecraft": plan["mc_version"]}
            if plan["loader"] != VANILLA:
                deps[mrpack_dependency_key(plan["loader"])] = plan["loader_version"]["value"]
            doc = {"formatVersion": 1, "game": "minecraft", "versionId": plan["version"], "name": plan["name"],
                   "files": entries, "dependencies": deps}
            if plan["summary"]:
                doc["summary"] = plan["summary"]
            return INDEX_NAME, doc
        entries = sorted((_curseforge_entry(pf) for pf in files), key=lambda e: (e["projectID"], e["fileID"]))
        loaders = []
        if plan["loader"] != VANILLA:
            loaders = [{"id": curseforge_loader_id(plan["loader"], plan["loader_version"]["value"]), "primary": True}]
        doc = {"minecraft": {"version": plan["mc_version"], "modLoaders": loaders}, "manifestType": "minecraftModpack",
               "manifestVersion": 1, "name": plan["name"], "version": plan["version"], "author": plan["author"],
               "files": entries, "overrides": OVERRIDES}
        return MANIFEST_NAME, doc
    except _Bad as bad:
        raise outdir.Refused(VERIFY_FAILED, "The pack was not written: a file in it is not valid (%s)." % bad.text) from None


def _info(name):
    info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3                                  # the same on every OS, so the bytes do not depend on it
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _expected_names(plan, manifest_name):
    names = {manifest_name}
    for rel, _abs, _size in (plan.get("overrides") or {}).get("files", []):
        names.add(OVERRIDES + "/" + rel)
    return names


def _build_zip(fh, plan):
    manifest_name, doc = build_manifest(plan)
    plan["_doc"] = doc
    with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_info(manifest_name), _json_bytes(doc))
        for rel, abs_path, size in (plan.get("overrides") or {}).get("files", []):
            arc = OVERRIDES + "/" + rel
            if not _member_ok(arc):
                raise outdir.Refused(VERIFY_FAILED, "An unsafe name in src (%s) stopped the pack; nothing was kept." % rel[:80])
            with _open_for_read(abs_path, size) as src:
                with zf.open(_info(arc), "w", force_zip64=size >= (1 << 31)) as dst:
                    left = size
                    while left > 0:
                        chunk = src.read(min(1 << 20, left))
                        if not chunk:
                            raise outdir.Refused(VERIFY_FAILED, "A file in src got shorter while the pack was built; "
                                                                "nothing was kept.")
                        dst.write(chunk)
                        left -= len(chunk)


# --------------------------------------------------------------------------- re-checking a pack

def _member_ok(name):
    if not isinstance(name, str) or not name or len(name) > 400 or "\\" in name or "\x00" in name or ":" in name:
        return False
    if name.startswith("/") or _DRIVE_RE.match(name):
        return False
    parts = name[:-1].split("/") if name.endswith("/") else name.split("/")
    return bool(parts) and all(p not in ("", ".", "..") and not _CTRL_RE.search(p) for p in parts)


def _check_index(doc):
    if not isinstance(doc, dict):
        return "the index isn't an object"
    if doc.get("formatVersion") != 1 or doc.get("game") != "minecraft":
        return "formatVersion/game are not 1/minecraft"
    for key in ("versionId", "name"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            return "%s is missing" % key
    deps = doc.get("dependencies")
    if not isinstance(deps, dict) or not isinstance(deps.get("minecraft"), str):
        return "dependencies.minecraft is missing"
    extra = [k for k in deps if k != "minecraft"]
    if len(extra) > 1 or any(k not in DEPENDENCY_KEYS or not isinstance(deps[k], str) for k in extra):
        return "dependencies must be minecraft plus at most one loader"
    files = doc.get("files")
    if not isinstance(files, list):
        return "files is not a list"
    seen = set()
    for item in files:
        if not isinstance(item, dict) or not _rel_path_ok(item.get("path")):
            return "a file has an unsafe or missing path"
        if item["path"].lower() in seen:
            return "two files share the path %s" % item["path"][:60]
        seen.add(item["path"].lower())
        hashes = item.get("hashes")
        if not isinstance(hashes, dict) or not _SHA1_RE.fullmatch(str(hashes.get("sha1") or "")) \
                or not _SHA512_RE.fullmatch(str(hashes.get("sha512") or "")):
            return "%s needs both sha1 and sha512" % item["path"][:60]
        downloads = item.get("downloads")
        if not isinstance(downloads, list) or not downloads \
                or any(not isinstance(u, str) or not _https_on(u, MRPACK_DOWNLOAD_HOSTS) for u in downloads):
            return "%s has no valid https download link" % item["path"][:60]
        size = item.get("fileSize")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return "%s has no valid fileSize" % item["path"][:60]
        env = item.get("env")
        if env is not None and (not isinstance(env, dict) or any(env.get(s) not in SIDES for s in ("client", "server"))):
            return "%s has an invalid env" % item["path"][:60]
    return ""


def _check_manifest(doc):
    if not isinstance(doc, dict):
        return "the manifest isn't an object"
    if doc.get("manifestType") != "minecraftModpack" or doc.get("manifestVersion") != 1:
        return "manifestType/manifestVersion are wrong"
    mc = doc.get("minecraft")
    if not isinstance(mc, dict) or not isinstance(mc.get("version"), str) or not isinstance(mc.get("modLoaders"), list):
        return "minecraft.version / modLoaders are missing"
    if any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in mc["modLoaders"]) \
            or sum(1 for m in mc["modLoaders"] if m.get("primary") is True) > 1:
        return "modLoaders is invalid"
    if doc.get("overrides") != OVERRIDES:
        return "overrides is not 'overrides'"
    for key in ("name", "version", "author"):
        if not isinstance(doc.get(key), str):
            return "%s is missing" % key
    files = doc.get("files")
    if not isinstance(files, list):
        return "files is not a list"
    for item in files:
        if not isinstance(item, dict) or any(
                not isinstance(item.get(k), int) or isinstance(item.get(k), bool) or item[k] <= 0
                for k in ("projectID", "fileID")) or not isinstance(item.get("required"), bool):
            return "a file entry is invalid"
    return ""


def check_pack(path, fmt, expected_names=None, expected_doc=None):
    """Re-open a pack and re-parse its manifest.  -> (True, "") | (False, why).  Never raises.

    Rejects: not a zip, a bad CRC, an entry name that could escape its folder (zip-slip: '..', a leading slash or drive,
    a backslash, a colon), a duplicate name (case-insensitive), a missing or oversized manifest, a manifest that is not
    valid for `fmt`; with `expected_names` / `expected_doc`, any difference from what was meant to be written."""
    try:
        if fmt not in FORMATS:
            return False, "unknown format"
        manifest_name = INDEX_NAME if fmt == "modrinth" else MANIFEST_NAME
        with zipfile.ZipFile(path) as zf:
            names = [i.filename for i in zf.infolist()]
            for name in names:
                if not _member_ok(name):
                    return False, "unsafe entry name %r" % name[:60]
            lowered = [n.lower() for n in names]
            if len(set(lowered)) != len(lowered):
                return False, "duplicate entry names"
            if manifest_name not in names:
                return False, "%s is missing at the top" % manifest_name
            info = zf.getinfo(manifest_name)
            if info.file_size > DEFENSIVE_MANIFEST_BYTES:
                return False, "the manifest is implausibly large"
            if zf.testzip() is not None:
                return False, "a file inside is damaged"
            try:
                doc = json.loads(zf.read(manifest_name).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return False, "the manifest doesn't parse"
        why = _check_index(doc) if fmt == "modrinth" else _check_manifest(doc)
        if why:
            return False, why
        if expected_names is not None and set(names) != set(expected_names):
            return False, "the files inside are not the ones that were meant to be written"
        if expected_doc is not None and doc != expected_doc:
            return False, "the manifest is not what was meant to be written"
        return True, ""
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError, NotImplementedError, ValueError):
        return False, "it isn't a readable zip"
    except Exception:  # noqa: BLE001
        return False, "it could not be checked"


# --------------------------------------------------------------------------- planning

class _Ctx:
    def __init__(self, platform, adapter, mc, loader, channel, deadline_seconds):
        self.platform, self.adapter, self.mc, self.loader, self.channel = platform, adapter, mc, loader, channel
        self.fallback = _quilt_fallback()
        self.started = CLOCK()
        self.deadline = deadline_seconds
        self.skipped, self.warnings, self.partial_files = [], [], []

    def tick(self):
        if self.deadline and CLOCK() - self.started > self.deadline:
            raise _TimeUp()

    def skip(self, ref, code, text=None, project=None, dependency=False, **extra):
        item = {"project": untrusted.clip(ref, 100), "reason": code}
        if project:
            item["name"] = project.get("name") or ""
            if project.get("slug"):
                item["slug"] = project["slug"]
            if project.get("page_url"):
                item["page_url"] = project["page_url"]
        if text:
            item["error"] = untrusted.clip(text, 220)
        if dependency:
            item["dependency"] = True
        item.update(extra)
        self.skipped.append(item)


_FATAL = ("no_key", "bad_key", "rate_limited", "offline")


def _mini(project):
    return {"name": project.get("name") or "", "slug": project.get("slug") or "", "id": project.get("id") or "",
            "page_url": project.get("page_url") or "", "kind": project.get("kind") or ""}


def _resolve_projects(ctx, entries):
    """-> ([(entry, project)], None) | (None, failure).  Bulk first; a ref the bulk call does not know goes through the
    never-guess rule (resolve.resolve_project): an exact id or slug, or the one project whose name matches exactly."""
    refs = []
    for e in entries:
        if e["ref"] not in refs:
            refs.append(e["ref"])
    got = ctx.adapter.bulk_projects(refs)
    if not got.get("ok"):
        return None, got
    ctx.tick()
    known = got.get("projects") or {}
    out = []
    for entry in entries:
        if entry["source"] and entry["source"] != ctx.platform:
            ctx.skip(entry["ref"], "wrong_platform",
                     "this entry is for %s but the pack is a %s pack: a pack lists one platform only"
                     % (entry["source"], ctx.platform))
            continue
        project = known.get(entry["ref"])
        if not project:
            found = resolve.resolve_project(ctx.platform, entry["ref"])
            ctx.tick()
            if found.get("ok"):
                project = found["project"]
            elif found.get("reason") in _FATAL:
                return None, found
            else:
                reason = found.get("reason") if found.get("reason") in (AMBIGUOUS, NOT_FOUND) else NOT_FOUND
                extra = {}
                if found.get("candidates"):
                    extra["candidates"] = [_mini(c) for c in found["candidates"][:5]]
                ctx.skip(entry["ref"], reason, found.get("error") or "not found on %s" % ctx.platform, **extra)
                continue
        if project.get("kind") == "modpack":
            ctx.skip(entry["ref"], "unsupported_kind", "a modpack can't go inside a modpack", project=project)
            continue
        out.append((entry, project))
    return out, None


def _merge_duplicates(ctx, pairs):
    """One entry per project.  The same project twice with the same (or no) pinned file is a duplicate, reported;
    two different pinned files for one project is an error.  -> (pairs, None) | (None, failure)."""
    by_id, order = {}, []
    for entry, project in pairs:
        pid = str(project["id"])
        if pid not in by_id:
            by_id[pid] = (entry, project)
            order.append(pid)
            continue
        first, _p = by_id[pid]
        if first["file"] and entry["file"] and first["file"] != entry["file"]:
            return None, fail(AMBIGUOUS, "%s is listed twice with different files (%s and %s); say which one the pack "
                                         "should use." % (project.get("name") or pid, first["file"], entry["file"]),
                              project=project.get("name") or pid)
        merged = dict(first)
        if not first["file"] and entry["file"]:
            merged["file"] = entry["file"]
        by_id[pid] = (merged, project)
        ctx.skip(entry["ref"], "duplicate", "already in the pack", project=project)
    return [by_id[p] for p in order], None


def _list_group(ctx, projects, loader):
    """Newest-first file lists for these projects (bulk).  -> ({project_id: [file]}, {project_id: reason}, None) | failure."""
    if not projects:
        return {}, {}, None
    got = ctx.adapter.bulk_list_files([str(p["id"]) for p in projects], mc_version=ctx.mc, loader=loader)
    if not got.get("ok"):
        return None, None, got
    ctx.tick()
    return got.get("files") or {}, got.get("errors") or {}, None


def _select_files(ctx, pairs):
    """pairs -> ([{"entry", "project", "file"}], None) | (None, failure).  Skips (with reasons) what cannot be picked."""
    chosen = {}                                            # project id -> dict
    fetch_loader = None if (ctx.loader == "quilt" and ctx.fallback) else ctx.loader
    pinned = [(e, p) for e, p in pairs if e["file"]]
    free_mods = [p for e, p in pairs if not e["file"] and p.get("kind") == "mod" and ctx.loader != VANILLA]
    free_other = [p for e, p in pairs if not e["file"] and p.get("kind") != "mod"]
    for entry, project in pairs:
        if not entry["file"] and project.get("kind") == "mod" and ctx.loader == VANILLA:
            ctx.skip(entry["ref"], "needs_loader", "a mod needs a loader; this pack was asked for with none", project=project)

    if pinned:
        got = ctx.adapter.bulk_get_files([(str(p["id"]), e["file"]) for e, p in pinned])
        if not got.get("ok"):
            return None, got
        ctx.tick()
        for entry, project in pinned:
            file = (got.get("files") or {}).get("%s:%s" % (project["id"], entry["file"]))
            if not file:
                ctx.skip(entry["ref"], NOT_FOUND, "that file id doesn't exist for this project", project=project)
                continue
            if project.get("kind") == "mod" and ctx.loader != VANILLA:
                if ctx.mc not in (file.get("mc_versions") or []) or ctx.loader not in (file.get("loaders") or []):
                    ctx.warnings.append("The file you pinned for %s isn't marked for Minecraft %s on %s; it was used "
                                        "as asked." % (project.get("name") or entry["ref"], ctx.mc, ctx.loader))
            chosen[str(project["id"])] = {"entry": entry, "project": project, "file": file}

    for projects, loader, pick_loader in ((free_mods, fetch_loader, ctx.loader), (free_other, None, None)):
        lists, errors, bad = _list_group(ctx, projects, loader)
        if bad:
            return None, bad
        for project in projects:
            entry = next(e for e, p in pairs if p is project)
            pid = str(project["id"])
            if pid in errors:
                ctx.skip(entry["ref"], errors[pid] if errors[pid] in ("rate_limited", "offline", "vendor_changed")
                         else VENDOR_CHANGED, "its files couldn't be listed", project=project)
                continue
            picked = resolve.pick_file(lists.get(pid) or [], ctx.mc, pick_loader, channel=entry["channel"] or ctx.channel,
                                       quilt_fallback=ctx.fallback)
            if not picked.get("ok"):
                extra = {}
                if picked.get("alternatives"):
                    extra["alternatives"] = [{"version": a.get("version_label") or "", "mc_versions": (a.get("mc_versions") or [])[:3],
                                              "loaders": a.get("loaders") or [], "channel": a.get("channel") or ""}
                                             for a in picked["alternatives"][:3]]
                ctx.skip(entry["ref"], NO_COMPATIBLE_FILE, picked.get("error") or "no compatible file", project=project, **extra)
                continue
            chosen[pid] = {"entry": entry, "project": project, "file": picked["file"]}
    return [chosen[str(p["id"])] for _e, p in pairs if str(p["id"]) in chosen], None


def _needed_by(files, project_id):
    for item in files:
        for dep in item["file"].get("dependencies") or []:
            if str(dep.get("project_id")) == str(project_id) and dep.get("type") == "required":
                return item["project"].get("name") or str(item["project"].get("id"))
    return None


def _add_dependencies(ctx, selected):
    """Required dependencies, once each.  -> (list of {"entry", "project", "file", "dependency_of"}, None) | failure."""
    result = ctx.adapter and resolve.add_dependencies(ctx.platform, [s["file"] for s in selected], ctx.mc, ctx.loader)
    ctx.tick()
    if not result or not result.get("ok"):
        if result and result.get("reason") in _FATAL:
            return None, result
        ctx.warnings.append("Required dependencies couldn't be worked out (%s); the pack lists only what you named."
                            % untrusted.clip((result or {}).get("error") or "unknown error", 120))
        return [], None
    added = result.get("added") or []
    for item in result.get("skipped") or []:
        ctx.skip(item.get("project_id") or "?", item.get("reason") or "no_compatible_file",
                 "a required dependency that couldn't be added", dependency=True)
    ids = sorted({str(f.get("project_id")) for f in added if f.get("project_id")})
    projects = {}
    if ids:
        got = ctx.adapter.bulk_projects(ids)
        ctx.tick()
        if got.get("ok"):
            projects = {k: v for k, v in (got.get("projects") or {}).items() if v}
        elif got.get("reason") in _FATAL:
            return None, got
    out = []
    for file in added:
        pid = str(file.get("project_id") or "")
        project = projects.get(pid) or {"id": pid, "name": pid, "slug": "", "kind": "mod", "page_url": "",
                                       "distributable": None}
        out.append({"entry": {"ref": pid, "required": True, "env": None}, "project": project, "file": file,
                    "dependency_of": _needed_by(selected, pid) or "a listed project"})
    return out, None


def _vet_all(ctx, selected, dependency):
    """selected -> ([pack file], [the selected items that passed]); whatever fails is skipped with its reason."""
    out, passed = [], []
    for item in selected:
        try:
            pf = _vet(ctx.platform, item["file"], item["project"], item["entry"], item["project"].get("kind") or "mod",
                      item.get("dependency_of"))
        except _Bad as bad:
            ctx.skip(item["entry"]["ref"] if not dependency else (item["project"].get("name") or item["entry"]["ref"]),
                     bad.code, bad.text, project=item["project"], dependency=dependency)
            continue
        out.append(pf)
        passed.append(item)
    return out, passed


def _drop_name_clashes(ctx, files):
    """The first of two files with the same pack path (case-insensitive, in a fixed order) stays; the rest are skipped and
    reported.  The list keeps the order the owner gave (the manifest sorts its own entries)."""
    seen, dropped = set(), set()
    for pf in sorted(files, key=lambda f: (f.get("path") or "", f["project_id"], f["file_id"])):
        key = (pf.get("path") or "").lower()
        if key and key in seen:
            ctx.skip(pf["name"], "name_clash", "another file in the pack already has the path %s" % pf["path"],
                     dependency=bool(pf.get("dependency_of")))
            dropped.add(id(pf))
            continue
        seen.add(key)
    return [pf for pf in files if id(pf) not in dropped]


def plan_pack(args):
    """Validate and resolve; never writes.  -> {"ok": True, "plan": plan} | a final result dict."""
    try:
        return _plan(args if isinstance(args, dict) else {})
    except Exception as exc:  # noqa: BLE001 - nothing here raises
        return fail(UNSUPPORTED, "The pack could not be planned (%s)." % type(exc).__name__)


def _plan(args):
    fmt = str(args.get("format") or "").strip().lower()
    if fmt not in FORMATS:
        return fail(UNSUPPORTED, "format must be 'modrinth' (a .mrpack) or 'curseforge' (a .zip).")
    name = _text(args.get("name"), NAME_LEN)
    if not name:
        return _clarify("What should the pack be called?")
    version = _text(args.get("version"), VERSION_LEN) or "1.0.0"
    author = _text(args.get("author"), AUTHOR_LEN)
    summary = _text(args.get("summary"), SUMMARY_LEN)
    if not str(args.get("mc_version") or "").strip():
        return _clarify("Which Minecraft version is the pack for (like 1.20.1 or 26.3)?")
    mc = resolve.normalise_mc_version(args.get("mc_version"))
    if mc is None:
        return fail(UNSUPPORTED, "'%s' isn't a Minecraft version like 1.20.1 or 26.3." % str(args.get("mc_version"))[:30])
    raw_loader = str(args.get("loader") or "").strip().lower()
    if not raw_loader:
        return _clarify("Which loader is the pack for (fabric, forge, neoforge or quilt)?")
    loader = VANILLA if raw_loader in (VANILLA, "none") else resolve.normalise_loader(raw_loader)
    if loader is None:
        return fail(UNSUPPORTED, "'%s' isn't a loader Jarvis knows (fabric, forge, neoforge, quilt)." % raw_loader[:30])
    channel = str(args.get("channel") or "release").strip().lower()
    if channel not in CHANNELS:
        return fail(UNSUPPORTED, "channel must be release, beta or alpha.")
    entries, bad = parse_entries(args.get("entries"))
    if bad:
        return bad
    cap = _cap("max_pack_files")
    if cap is not None and len(entries) > cap:
        return fail(TOO_BIG, "%d entries is more than the pack file limit (%d, a tool setting)." % (len(entries), cap))

    folder, bad = outdir.resolve_dest(args.get("dest"), _default_dest())
    if bad:
        return bad
    stem = re.sub(r"[\\/]+", "_", "%s-%s" % (name, version))        # a slash in the name is not a folder
    filename, bad = outdir.pack_filename(args.get("filename"), stem, EXTENSION[fmt])
    if bad:
        return bad
    overwrite = _flag(args.get("overwrite"), False)
    target = folder / filename
    if os.path.lexists(target):
        if not overwrite:
            return fail("exists", "%s already exists; nothing was done. Give another filename, or overwrite=true to "
                                  "replace it." % str(target)[:160], path=str(target))
        if outdir.is_link(target) or not os.path.isfile(target):
            return fail(BAD_DEST, "%s is not a plain file, so it is never replaced." % str(target)[:160])

    overrides = None
    if args.get("src") not in (None, ""):
        overrides, bad = scan_overrides(args["src"], folder)
        if bad:
            return bad

    loader_version = None
    if loader != VANILLA:
        loader_version, bad = work_out_loader_version(loader, mc, args.get("loader_version"))
        if bad:
            return bad

    adapter = sources.get(fmt)
    if adapter is None:
        return fail(UNSUPPORTED, "%s support is not installed here, so a %s pack can't be made." %
                    ("CurseForge" if fmt == "curseforge" else fmt.capitalize(), fmt))
    try:
        seconds = float(_setting("deadline_seconds", 0) or 0)
    except (TypeError, ValueError):
        seconds = 0
    ctx = _Ctx(fmt, adapter, mc, loader, channel, seconds)
    plan = {"format": fmt, "name": name, "version": version, "author": author, "summary": summary, "mc_version": mc,
            "loader": loader, "loader_version": loader_version, "folder": folder, "filename": filename,
            "target": target, "overwrite": overwrite, "overrides": overrides, "entries": entries, "files": [],
            "skipped": ctx.skipped, "warnings": ctx.warnings, "include_dependencies": _flag(args.get("include_dependencies"), True)}
    try:
        pairs, bad = _resolve_projects(ctx, entries)
        if bad:
            return bad
        pairs, bad = _merge_duplicates(ctx, pairs)
        if bad:
            return bad
        selected, bad = _select_files(ctx, pairs)
        if bad:
            return bad
        files, passed = _vet_all(ctx, selected, False)
        deps = []
        if plan["include_dependencies"] and loader != VANILLA and passed:
            added, bad = _add_dependencies(ctx, passed)        # only files that made it in bring their dependencies
            if bad:
                return bad
            deps, _ok = _vet_all(ctx, added, True)
        elif plan["include_dependencies"] and loader == VANILLA and passed:
            ctx.warnings.append("No loader was given, so required dependencies were not looked up.")
        everything = _drop_name_clashes(ctx, files + deps)
    except _TimeUp:
        plan["files"] = []
        return {"ok": True, "partial": True, "written": False, "dry_run": bool(args.get("dry_run")), "installed": False,
                "format": fmt, "name": name,
                "message": "Stopped at the time limit (%d s, a tool setting) before the pack was fully worked out. "
                           "Nothing was written. Ask again, or raise the limit." % int(ctx.deadline),
                "skipped": ctx.skipped[:REPORT_SKIPPED_MAX]}
    cap = _cap("max_pack_files")
    if cap is not None and len(everything) > cap:
        return fail(TOO_BIG, "The pack would hold %d files, more than the pack file limit (%d, a tool setting)."
                    % (len(everything), cap))
    plan["files"] = everything
    if not everything and not (overrides and overrides["files"]):
        return fail(NO_COMPATIBLE_FILE, "Nothing could be put in the pack: every entry was skipped (see skipped).",
                    skipped=ctx.skipped[:REPORT_SKIPPED_MAX], written=False)
    if not everything:
        ctx.warnings.append("No project files could be listed; the pack holds only the overrides.")
    _conflicts(everything, ctx.warnings)
    return {"ok": True, "plan": plan}


def _conflicts(files, warnings):
    """A file that lists another included project as incompatible: say so (the pack is still written as asked)."""
    names = {pf["project_id"]: pf["name"] for pf in files}
    for pf in files:
        for other in pf.get("incompatible") or []:
            if other in names and other != pf["project_id"]:
                warnings.append("%s says it is incompatible with %s, and both are in the pack." % (pf["name"], names[other]))


# --------------------------------------------------------------------------- the report and the write

def _file_view(pf, platform):
    view = {"project": pf["name"], "slug": pf["slug"], "project_id": pf["project_id"], "file_id": pf["file_id"],
            "file_name": pf["file_name"], "version": pf["version"], "channel": pf["channel"], "kind": pf["kind"]}
    if pf.get("via"):
        view["via"] = pf["via"]
        view["via_note"] = "a Fabric file, offered because the project has no Quilt file"
    if platform == "modrinth":
        view["path"] = pf["path"]
        if pf.get("env"):
            view["env"] = pf["env"]
    else:
        view["required"] = bool(pf.get("required", True))
    if pf.get("dependency_of"):
        view["needed_by"] = pf["dependency_of"]
    return view


def describe(plan, dry_run, written=None):
    """The owner-facing answer for a plan: what was resolved, what was added as a dependency, what was skipped and why."""
    platform = plan["format"]
    listed = [pf for pf in plan["files"] if not pf.get("dependency_of")]
    added = [pf for pf in plan["files"] if pf.get("dependency_of")]
    skipped = plan["skipped"]
    ov = plan.get("overrides")
    lv = plan.get("loader_version")
    result = {
        "ok": True, "dry_run": bool(dry_run), "written": bool(written), "installed": False, "format": platform,
        "source": platform, "name": plan["name"], "version": plan["version"], "mc_version": plan["mc_version"],
        "loader": plan["loader"], "path": str(plan["target"]), "dest": str(plan["folder"]), "filename": plan["filename"],
        "counts": {"resolved": len(listed), "dependencies": len(added), "skipped": len(skipped),
                   "override_files": len(ov["files"]) if ov else 0},
        "complete": not skipped,
        "resolved": [_file_view(pf, platform) for pf in listed],
        "dependencies_added": [_file_view(pf, platform) for pf in added],
        "skipped": skipped[:REPORT_SKIPPED_MAX],
    }
    if lv:
        result["loader_version"] = lv["value"]
        result["loader_version_source"] = lv["source"]
        if lv.get("resolved_as"):
            result["loader_version_resolved_as"] = lv["resolved_as"]
        if lv.get("note"):
            result["loader_version_note"] = lv["note"]
    if len(skipped) > REPORT_SKIPPED_MAX:
        result["skipped_more"] = len(skipped) - REPORT_SKIPPED_MAX
    if ov:
        result["overrides"] = {"folder": str(ov["folder"]), "files": len(ov["files"]), "bytes": ov["bytes"],
                               "skipped": ov["skipped"][:REPORT_SKIPPED_MAX], "skipped_count": ov["skipped_count"]}
    if plan["warnings"]:
        result["warnings"] = list(plan["warnings"])[:20]
    what = "%d project file%s" % (len(listed), "" if len(listed) == 1 else "s")
    if added:
        what += " plus %d required dependenc%s" % (len(added), "y" if len(added) == 1 else "ies")
    if ov:
        what += " and %d override file%s from %s" % (len(ov["files"]), "" if len(ov["files"]) == 1 else "s", ov["folder"])
    lines = ""
    if lv:
        lines = " Loader %s %s (%s)." % (plan["loader"], lv["value"], lv["source"])
    left = " %d entr%s skipped (see skipped)." % (len(skipped), "y was" if len(skipped) == 1 else "ies were") if skipped else ""
    if written:
        result["message"] = ("Wrote %s: %s.%s%s Written only: nothing was installed or opened." % (plan["target"], what, lines, left))
    elif dry_run:
        result["message"] = ("Dry run: would write %s with %s.%s%s Nothing was written or created." % (plan["target"], what, lines, left))
    else:
        result["message"] = "Planned %s with %s.%s%s" % (plan["target"], what, lines, left)
    texts = []
    for pf in plan["files"]:
        texts += [pf["name"], pf["file_name"], pf["version"]]
    for item in skipped:
        texts += [item.get("name"), item.get("error")]
    return untrusted.mark(result, *texts)


def write_pack(plan):
    """Write the planned pack.  -> the describe() result with written=True, or a failure (nothing left behind)."""
    try:
        manifest_name = INDEX_NAME if plan["format"] == "modrinth" else MANIFEST_NAME
        names = _expected_names(plan, manifest_name)

        def build(fh):
            _build_zip(fh, plan)

        def validate(path):
            return check_pack(path, plan["format"], names, plan.get("_doc"))

        got = outdir.write_atomic(plan["folder"], plan["filename"], build, validate, overwrite=plan["overwrite"])
        if not got.get("ok"):
            got.setdefault("written", False)
            return got
        result = describe(plan, False, written=True)
        result["size"] = got["size"]
        if got.get("created_folder"):
            result["created_folder"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - this never raises
        return fail(UNSUPPORTED, "The pack could not be written (%s). Nothing was kept." % type(exc).__name__)
