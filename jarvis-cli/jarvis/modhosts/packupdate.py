"""Write a NEW modpack file with newer files, from an existing pack (Minecraft tools, agent M4).

actions/minecraft_packread.py is the tool (`update_modpack_file`); this file is everything it needs.

WHAT IT DOES
  prepare(args)    reads the pack (modhosts/packread.py: in memory, hard limits, nothing extracted), finds for every entry
                   the newest compatible file ON THE PACK'S OWN PLATFORM, and returns {"ok": True, "prepared": P} or a final
                   result (a failure, or "nothing to update").  It never writes.  The network is used only through the
                   adapters (modhosts/sources.py), the same ones make_modpack_file uses.
  describe(P, dry_run, written)   the owner-facing report: unchanged / updated (old -> new) / skipped with the reason /
                   dependencies added / overrides.
  write(P)         writes ONE new file in the destination folder with the pack writer itself (packs.write_pack, which goes
                   through outdir.write_atomic with overwrite=False): a temp file, re-opened and re-parsed, then moved into
                   place without ever replacing anything.  The old pack's overrides are a zip source of that writer
                   (packs.overrides_from_zip): there is no copy code in this file.

THE RULES
  * The original pack is never modified, moved or deleted: it is opened for reading, twice (once by packread to read it, once
    by the pack writer to copy its overrides), and nothing else touches it.  There is no `overwrite` argument anywhere: the new file is
    `<name>-updated.mrpack` / `.zip`, and when that name is taken `-updated-2`, `-updated-3` ... (never a replacement; the
    write itself refuses an existing name).
  * "Newer" means a later PUBLICATION DATE of a file for the SAME Minecraft version and loader, never a bigger version
    number.  The file is chosen with resolve.pick_file (the rules make_modpack_file uses: exact Minecraft version, strict
    loader, Quilt may take Fabric files when the setting allows, release before beta before alpha inside the channel the
    owner accepts).  An installed file that is as new as the candidate, or newer, is `unchanged`: nothing is ever swapped
    for an older file.
  * A file is found by its hash (Modrinth: SHA-1 through POST /version_files) or by its ids (CurseForge: projectID and
    fileID), never by its name.  An entry that is not found, a project that is ambiguous, a file whose author blocks
    third-party downloads, a project with no compatible file: that entry is SKIPPED with its reason and STAYS AS IT WAS in
    the new pack, so the new pack is never smaller than the old one by accident.  An entry that cannot be carried over into
    a pack the writer accepts (a Modrinth link that is not on cdn.modrinth.com, a missing hash, an unsafe path) is a
    BLOCKER: the new pack is not written at all and the report says which entries and why.  With `allow_drop` (default
    false, an argument of update_modpack_file) the entries that cannot be carried are LEFT OUT instead: the pack is written
    without them and every one is listed under `dropped` (a dry run lists them too).  It never drops what merely clashes
    (two files with one path), and it never writes a pack that would have nothing left in it.
  * Dependencies the newer files require are added the way make_modpack_file adds them (packs.plan_pack, which calls
    resolve.add_dependencies) and listed under `dependencies_added`.  A dependency the pack already holds is not added twice.
  * Overrides are copied from the old pack UNCHANGED, member by member, zip to zip, never to disk and never interpreted:
    the bytes are streamed through, the CRC is re-checked, and a member that changed since it was listed stops the write.
    That is the pack writer's zip source (packs.overrides_from_zip + packs.write_pack), with the writer's own name rules and
    the three overrides caps (tool settings, 0 = none).
    (A CurseForge pack's overrides folder is written as `overrides/`, the name the manifest then says.)  Members that are
    neither the index nor an override (a README, an icon, CurseForge's modlist.html, which would be out of date) are not
    copied and are counted in `not_copied`.
  * Nothing a pack contains is run, installed or followed; no URL from a pack is fetched.

Every public function returns a plain dict and never raises.  Names and file names are stranger text: clipped, and the
result carries `untrusted_note`.
"""
import os
import re
from datetime import datetime
from urllib.parse import urlsplit

from . import (BAD_DEST, NO_COMPATIBLE_FILE, TOO_BIG, UNSUPPORTED, VERIFY_FAILED, fail, outdir, packread, packs,
               resolve, sources, untrusted)

SKIP_REASONS = ("not_found", "ambiguous", "blocked", "no_compatible_file", "lookup_failed")
REPORT_LIST_MAX = 200
NUMBERED_NAMES = 99

CLOCK = packs.CLOCK                      # tests replace packs.CLOCK or this one to hit the time limit without waiting


class _TimeUp(Exception):
    pass


# --------------------------------------------------------------------------- settings (the group's, read-only here)

def _setting(key, default=0):
    try:
        from .. import tool_settings
        return tool_settings.get("minecraft_lookup", key, default)
    except Exception:  # noqa: BLE001
        return default


def _default_dest():
    raw = _setting("default_dest", "~/Downloads")
    return raw.strip() if isinstance(raw, str) and raw.strip() else "~/Downloads"


def _cap(key):
    try:
        value = float(_setting(key, 0) or 0)
    except (TypeError, ValueError):
        return None
    return int(value) if value > 0 else None


def _quilt_fallback():
    value = _setting("quilt_fabric_fallback", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def _deadline():
    try:
        return max(0.0, float(_setting("deadline_seconds", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- small helpers

def _when(text):
    try:
        return datetime.fromisoformat(str(text or "").replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _name(value, limit=untrusted.NAME_LEN):
    return untrusted.clean_name("" if value is None else str(value), limit)


def _flag(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


class _Run:
    """The clock of one run: a tool setting (`deadline_seconds`, 0 = none) stops it between steps."""

    def __init__(self):
        self.started, self.limit = CLOCK(), _deadline()

    def tick(self):
        if self.limit and CLOCK() - self.started > self.limit:
            raise _TimeUp()


# --------------------------------------------------------------------------- entries

def _https_on(url, allowed):
    """https, a host on `allowed`, no user part, no control character: the test the pack writer applies to a link."""
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and (parts.hostname or "").lower() in allowed and not parts.username
                and not re.search(r"[\x00-\x1f\x7f]", url))
    except ValueError:
        return False


def _mrpack_url(urls):
    """The first link of an entry that the writer accepts (https on cdn.modrinth.com), or None."""
    for url in urls or []:
        if isinstance(url, str) and _https_on(url, packs.MRPACK_DOWNLOAD_HOSTS):
            return url
    return None


def _carry(fmt, view, raw):
    """The pack-file dict that keeps an entry exactly as it was -> (pf, None) | (None, why).  The pack writer will
    accept it (or `build_manifest` would refuse the whole pack), so what it cannot accept is a blocker."""
    if not isinstance(raw, dict):
        return None, "it isn't a readable entry"
    flags = set(raw.get("flags") or [])
    if fmt == "modrinth":
        if flags & {"unsafe_path", "duplicate_path"}:
            return None, "its path is unsafe or used twice, so the writer would refuse it"
        if not raw.get("sha1") or not raw.get("sha512"):
            return None, "it has no sha1 and sha512 hashes, and a .mrpack needs both"
        if raw.get("size") is None:
            return None, "its size isn't known, and a .mrpack needs it"
        if "bad_env" in flags:
            return None, "its client/server flags aren't valid"
        url = _mrpack_url(raw.get("urls"))
        if not url:
            return None, ("it has no https link on cdn.modrinth.com, the only host a pack written by Jarvis may list "
                          "(links elsewhere are never copied)")
        return {"project_id": "", "file_id": "", "kind": "mod", "name": view.get("file_name") or raw["path"],
                "slug": "", "file_name": view.get("file_name") or "", "version": "", "channel": "release",
                "mc_versions": [], "loaders": [], "download_url": url,
                "hashes": {"sha1": raw["sha1"], "sha512": raw["sha512"]}, "size": raw["size"], "path": raw["path"],
                "env": raw.get("env"), "page_url": "", "incompatible": []}, None
    if flags & {"bad_ids", "bad_required"}:
        return None, "its project/file ids or its required flag aren't valid"
    return {"project_id": str(raw["project_id"]), "file_id": str(raw["file_id"]), "kind": "mod",
            "name": str(raw["project_id"]), "slug": "", "file_name": "", "version": "", "channel": "release",
            "mc_versions": [], "loaders": [], "download_url": None, "hashes": {}, "size": None,
            "required": bool(raw["required"]) if raw.get("required") is not None else True, "page_url": "",
            "incompatible": []}, None


_HEX1, _HEX512 = re.compile(r"^[0-9a-f]{40}$"), re.compile(r"^[0-9a-f]{128}$")


def _from_record(e):
    """An entry the pack lists with a link the writer would refuse, but that Modrinth knows: the same file, written with
    Modrinth's own link and hashes (the pack's own path is kept when it is safe).  -> pf | None."""
    raw, file = e["raw"] or {}, e["installed"] or {}
    flags = set(raw.get("flags") or [])
    hashes = file.get("hashes") or {}
    if (not isinstance(raw.get("path"), str) or flags & {"unsafe_path", "duplicate_path", "bad_env"}
            or not _HEX1.match(str(hashes.get("sha1") or "")) or not _HEX512.match(str(hashes.get("sha512") or ""))
            or not isinstance(file.get("size"), int) or not _https_on(str(file.get("download_url") or ""),
                                                                      packs.MRPACK_DOWNLOAD_HOSTS)):
        return None
    return {"project_id": str(file.get("project_id") or ""), "file_id": str(file.get("file_id") or ""), "kind": "mod",
            "name": e["view"].get("file_name") or raw["path"], "slug": "", "file_name": e["view"].get("file_name") or "",
            "version": "", "channel": "release", "mc_versions": [], "loaders": [], "download_url": file["download_url"],
            "hashes": {"sha1": hashes["sha1"], "sha512": hashes["sha512"]}, "size": file["size"], "path": raw["path"],
            "env": raw.get("env"), "page_url": "", "incompatible": []}


def _entries(fmt, info):
    inner = info["_internal"]
    out = []
    for index, (view, raw) in enumerate(zip(info["files"], inner["files"])):
        e = {"i": index, "view": view, "raw": raw, "state": None, "reason": None, "error": None, "project": None,
             "installed": None, "new": None, "note": None, "page_url": None}
        e["pf"], e["carry_error"] = _carry(fmt, view, raw)
        out.append(e)
    return out


def _skip(e, reason, text, project=None, **extra):
    e["state"], e["reason"], e["error"] = "skipped", reason, untrusted.clip(text, 220)
    if project:
        e["project"] = project
        if project.get("page_url"):
            e["page_url"] = project["page_url"]
    e.update(extra)


def _label(e):
    if e["project"] and e["project"].get("name"):
        return _name(e["project"]["name"])
    view = e["view"]
    return view.get("file_name") or (str(view.get("project_id")) if view.get("project_id") else "") or "entry %d" % (e["i"] + 1)


def _old_name(e):
    return (e["installed"] or {}).get("file_name") or e["view"].get("file_name") or ""


# --------------------------------------------------------------------------- finding the newest file

def _identify(fmt, adapter, entries, run):
    """Which project and file is each entry?  Fills e["installed"] / e["project"] or skips.  -> failure | None."""
    if fmt == "modrinth":
        todo = [e for e in entries if e["raw"] and e["raw"].get("sha1")]
        for e in entries:
            if e not in todo:
                _skip(e, "not_found", "it has no usable SHA-1 hash, and a file is only ever matched by its content")
        hashes = sorted({e["raw"]["sha1"] for e in todo})
        got = adapter.versions_by_hashes(hashes, "sha1") if hashes else {"ok": True, "files": {}, "primary": {}}
        if not got.get("ok"):
            return got
        run.tick()
        found, primary = got.get("files") or {}, got.get("primary") or {}
        for e in todo:
            file = found.get(e["raw"]["sha1"])
            if not isinstance(file, dict):
                _skip(e, "not_found", "Modrinth doesn't know this file's hash (a file from somewhere else, or one that "
                                      "was removed); it stays as it was")
            elif not primary.get(e["raw"]["sha1"]):
                _skip(e, "ambiguous", "this is a secondary file of its Modrinth version, and a pack lists a version's "
                                      "main file only; it stays as it was")
            else:
                e["installed"] = file
        return None
    todo = [e for e in entries if e["raw"] and not (set(e["raw"].get("flags") or []) & {"bad_ids"})]
    for e in entries:
        if e not in todo:
            _skip(e, "not_found", "its project or file id isn't valid")
    pairs = [(e["raw"]["project_id"], e["raw"]["file_id"]) for e in todo]
    got = adapter.bulk_get_files(pairs) if pairs else {"ok": True, "files": {}}
    if not got.get("ok"):
        return got
    run.tick()
    for e in todo:
        file = (got.get("files") or {}).get("%s:%s" % (e["raw"]["project_id"], e["raw"]["file_id"]))
        if isinstance(file, dict):
            e["installed"] = file
        else:
            _skip(e, "not_found", "CurseForge doesn't list this file any more; it stays as it was")
    return None


def _name_projects(adapter, entries, run):
    ids = sorted({str(e["installed"]["project_id"]) for e in entries if e["installed"] and e["installed"].get("project_id")})
    if not ids:
        return {}, None
    got = adapter.bulk_projects(ids)
    if not got.get("ok"):
        return None, got
    run.tick()
    return {k: v for k, v in (got.get("projects") or {}).items() if v}, None


def _choose(fmt, adapter, entries, projects, mc, loader, same_target, channel, run):
    """For each identified entry: unchanged, updated (e["new"]) or skipped.  -> failure | None."""
    fallback = _quilt_fallback()
    fetch_loader = None if (loader == "quilt" and fallback) else loader
    seen = {}
    wanted_mods, wanted_other = [], []
    for e in entries:
        if e["state"] or not e["installed"]:
            continue
        pid = str(e["installed"].get("project_id") or "")
        project = projects.get(pid)
        e["project"] = project
        if project and project.get("page_url"):
            e["page_url"] = project["page_url"]
        if not project:
            _skip(e, "not_found", "the project of this file couldn't be looked up; it stays as it was")
            continue
        if project.get("kind") == "modpack":
            _skip(e, "ambiguous", "a modpack can't go inside a modpack; it stays as it was", project=project)
            continue
        if pid in seen:
            _skip(e, "ambiguous", "the same project is listed twice in the pack, so only the first entry is updated; "
                                  "this one stays as it was", project=project)
            continue
        seen[pid] = e
        if project.get("kind") == "mod":
            if loader == packs.VANILLA:
                _skip(e, "no_compatible_file", "a mod needs a loader and the pack has none; it stays as it was",
                      project=project)
                continue
            wanted_mods.append(project)
        else:
            wanted_other.append(project)
    for projects_group, group_loader, pick_loader in ((wanted_mods, fetch_loader, loader), (wanted_other, None, None)):
        if not projects_group:
            continue
        got = adapter.bulk_list_files([str(p["id"]) for p in projects_group], mc_version=mc, loader=group_loader)
        if not got.get("ok"):
            return got
        run.tick()
        lists, errors = got.get("files") or {}, got.get("errors") or {}
        for project in projects_group:
            pid = str(project["id"])
            e = seen[pid]
            if pid in errors:
                cause = errors[pid] if errors[pid] in ("rate_limited", "offline", "vendor_changed") else "vendor_changed"
                _skip(e, "lookup_failed", "its files couldn't be listed (%s); it stays as it was" % cause.replace("_", " "),
                      project=project, cause=cause)
                continue
            picked = resolve.pick_file(lists.get(pid) or [], mc, pick_loader, channel=channel, quilt_fallback=fallback)
            if not picked.get("ok"):
                _skip(e, "no_compatible_file", (picked.get("error") or "no compatible file") + " It stays as it was.",
                      project=project)
                continue
            _decide(e, picked["file"], project, same_target)
    return None


def _decide(e, new, project, same_target):
    old = e["installed"]
    if str(new.get("file_id")) == str(old.get("file_id")):
        e["state"] = "unchanged"
        return
    new_when, old_when = _when(new.get("date")), _when(old.get("date"))
    if same_target and old_when > 0 and new_when > 0 and new_when <= old_when:
        e["state"], e["note"] = "unchanged", "the file in the pack is as new as, or newer than, the newest one in the chosen channel"
        return
    if same_target and new_when <= 0:
        _skip(e, "ambiguous", "the candidate's publication date isn't known, so Jarvis can't tell that it is newer; it "
                              "stays as it was", project=project)
        return
    if project.get("distributable") is False or not new.get("downloadable"):
        _skip(e, "blocked", "the author doesn't allow third-party downloads of the newer file (or it has no download "
                            "link); it stays as it was" + (" See the project page." if project.get("page_url") else ""),
              project=project)
        return
    e["state"], e["new"] = "updated", new


# --------------------------------------------------------------------------- the plan

def _clarify_args(args):
    """-> ((mc, loader, channel), None) | (None, failure): the arguments that change what the pack is for."""
    channel = str(args.get("channel") or "release").strip().lower()
    if channel not in packs.CHANNELS:
        return None, fail(UNSUPPORTED, "channel must be release, beta or alpha.")
    mc = loader = None
    if args.get("mc_version") not in (None, ""):
        mc = resolve.normalise_mc_version(args.get("mc_version"))
        if mc is None:
            return None, fail(UNSUPPORTED, "'%s' isn't a Minecraft version like 1.20.1 or 26.3." % str(args["mc_version"])[:30])
    if args.get("loader") not in (None, ""):
        text = str(args["loader"]).strip().lower()
        loader = packs.VANILLA if text in (packs.VANILLA, "none") else resolve.normalise_loader(text)
        if loader is None:
            return None, fail(UNSUPPORTED, "'%s' isn't a loader Jarvis knows (fabric, forge, neoforge, quilt)." % text[:30])
    return (mc, loader, channel), None


def _new_filename(folder, base, extension):
    """`<base>-updated<ext>`, numbered when taken.  -> (filename, None) | (None, failure).  Nothing is created."""
    for number in range(1, NUMBERED_NAMES + 1):
        stem = "%s-updated" % base if number == 1 else "%s-updated-%d" % (base, number)
        filename, bad = outdir.pack_filename(None, stem, extension)
        if bad:
            return None, bad
        if not os.path.lexists(folder / filename):
            return filename, None
    return None, fail(BAD_DEST, "There are already %d updated copies of that pack in %s; pick another folder or name."
                      % (NUMBERED_NAMES, str(folder)[:100]))


def prepare(args):
    """Read, resolve, plan.  Never writes.  -> {"ok": True, "prepared": P} | a final result."""
    try:
        return _prepare(args if isinstance(args, dict) else {})
    except _TimeUp:
        return {"ok": True, "partial": True, "written": False, "installed": False,
                "message": "Stopped at the time limit (%d s, a tool setting) before the new pack was worked out. Nothing "
                           "was written. Ask again, or raise the limit." % int(_deadline())}
    except Exception as exc:  # noqa: BLE001 - nothing here raises
        return fail(UNSUPPORTED, "The pack couldn't be updated (%s). Nothing was written." % type(exc).__name__)


def _prepare(args):
    run = _Run()
    path, bad = packread.check_path(args.get("path"))
    if bad:
        return bad
    given, bad = _clarify_args(args)
    if bad:
        return bad
    given_mc, given_loader, channel = given
    info = packread.read_pack(path, internal=True)
    if not info.get("ok"):
        info.pop("_internal", None)
        return info
    fmt = info["format"]
    mc = given_mc or info.get("mc_version")
    loader = given_loader or info.get("loader")
    if not mc:
        return fail(UNSUPPORTED, "The pack doesn't say which Minecraft version it is for; give mc_version.")
    if not loader:
        return fail(UNSUPPORTED, "The pack doesn't say which loader it uses (or says several); give loader "
                                 "(fabric, forge, neoforge, quilt or vanilla).")
    same_target = mc == info.get("mc_version") and loader == info.get("loader")
    folder, bad = outdir.resolve_dest(args.get("dest"), _default_dest())
    if bad:
        return bad
    base_text = args.get("name") if args.get("name") not in (None, "") else path.stem
    pack_name = (untrusted.clip(str(args.get("name")), packs.NAME_LEN) if args.get("name") not in (None, "")
                 else info.get("name"))
    base = re.sub(r"[\\/]+", "_", str(base_text)).strip() or "modpack"
    filename, bad = _new_filename(folder, base, packs.EXTENSION[fmt])
    if bad:
        return bad
    target = folder / filename                       # a name that does not exist yet (_new_filename checked)

    loader_version = None
    if loader != packs.VANILLA:
        if same_target and info.get("loader_version"):
            if not packs.valid_loader_version(info["loader_version"]):
                return fail(NO_COMPATIBLE_FILE, "The pack's own %s version isn't one Jarvis can write back; give loader "
                                                "details by making a pack with make_modpack_file." % loader)
            loader_version = {"value": info["loader_version"], "source": "pack"}
        else:
            loader_version, bad = packs.work_out_loader_version(loader, mc, None)
            if bad:
                return bad
        run.tick()

    adapter = sources.get(fmt)
    if adapter is None:
        return fail(UNSUPPORTED, "%s support is not installed here, so a %s pack can't be updated." %
                    ("CurseForge" if fmt == "curseforge" else "Modrinth", fmt))
    if fmt == "modrinth" and not hasattr(adapter, "versions_by_hashes"):
        return fail(UNSUPPORTED, "Modrinth's hash lookup isn't available in this build, so a pack can't be updated.")

    entries = _entries(fmt, info)
    if not entries:
        return _nothing(info, fmt, path, mc, loader, "The pack lists no project files, so there is nothing to update.")
    bad = _identify(fmt, adapter, entries, run)
    if bad:
        return bad
    projects, bad = _name_projects(adapter, entries, run)
    if bad:
        return bad
    bad = _choose(fmt, adapter, entries, projects, mc, loader, same_target, channel, run)
    if bad:
        return bad

    updated = [e for e in entries if e["state"] == "updated"]
    added, plan_skipped, plan_warnings = [], [], []
    plan_files = {}
    if updated:
        request = {"format": fmt, "name": pack_name or "pack", "version": info.get("version") or "1.0.0",
                   "author": info.get("author") or "", "summary": info.get("summary") or "", "mc_version": mc,
                   "loader": loader, "channel": channel, "dest": str(folder), "filename": filename,
                   "include_dependencies": True,
                   "entries": [_pinned(fmt, e) for e in updated]}
        if loader_version:
            request["loader_version"] = loader_version["value"]
        planned = packs.plan_pack(request)
        run.tick()
        plan = planned.get("plan") if isinstance(planned, dict) else None
        if plan:
            plan_skipped, plan_warnings = list(plan["skipped"]), list(plan["warnings"])
            for pf in plan["files"]:
                plan_files[(str(pf["project_id"]), str(pf["file_id"]))] = pf
        elif isinstance(planned, dict) and planned.get("reason") == NO_COMPATIBLE_FILE and planned.get("skipped"):
            plan_skipped = list(planned["skipped"])               # none of the newer files could be put in a pack
        elif isinstance(planned, dict) and planned.get("partial"):
            raise _TimeUp()
        else:
            return planned
        for e in updated:
            pf = plan_files.get((str(e["new"]["project_id"]), str(e["new"]["file_id"])))
            if pf is None or pf.get("dependency_of"):
                why = next((s for s in plan_skipped if s.get("project") == str(e["new"]["project_id"]) and not s.get("dependency")), None)
                _skip(e, "blocked", ((why or {}).get("error") or "the pack writer would not take the newer file") +
                      "; it stays as it was", project=e["project"])
                e["new"] = None
            else:
                e["new_pf"] = pf
        kept_projects = {str(e["installed"]["project_id"]) for e in entries
                         if e["state"] != "updated" and e["installed"] and e["installed"].get("project_id")}
        for pf in plan["files"] if plan else []:
            if pf.get("dependency_of") and str(pf["project_id"]) not in kept_projects:
                added.append(pf)
        plan_skipped = [s for s in plan_skipped if s.get("dependency")]

    updated = [e for e in entries if e["state"] == "updated"]
    unchanged = [e for e in entries if e["state"] == "unchanged"]
    skipped = [e for e in entries if e["state"] == "skipped"]
    inner = info["_internal"]
    if not updated and not added and same_target:
        return _nothing(info, fmt, path, mc, loader, None, entries=entries, inner=inner)

    # What goes in the new pack: every entry, in the old order (updated ones with their new file), then the dependencies.
    files, blockers, relinked, uncarried = [], [], 0, []
    for e in entries:
        if e["state"] == "updated":
            files.append(e["new_pf"])
            continue
        if e["pf"] is None and fmt == "modrinth":
            e["pf"] = _from_record(e)                    # Modrinth knows the file: its own record has a link the writer takes
            if e["pf"] is not None:
                relinked += 1
        if e["pf"] is None:
            blocker = {"project": _label(e), "reason": "cannot_carry", "error": untrusted.clip(e["carry_error"], 200)}
            blockers.append(blocker)
            uncarried.append((e, blocker))
            continue
        pf = dict(e["pf"])
        if e["installed"]:
            pf["project_id"] = str(e["installed"].get("project_id") or pf["project_id"])
            pf["file_id"] = str(e["installed"].get("file_id") or pf["file_id"])
            pf["version"] = e["installed"].get("version_label") or ""
        if e["project"]:
            pf["name"] = e["project"].get("name") or pf["name"]
            pf["slug"] = e["project"].get("slug") or ""
        files.append(pf)
    files += added
    if relinked:
        P_warn = ("%d file%s listed a download link that Jarvis never writes; Modrinth knows the same file by its hash, so "
                  "the new pack uses Modrinth's own link for %s." % (relinked, "" if relinked == 1 else "s",
                                                                     "it" if relinked == 1 else "them"))
    else:
        P_warn = None
    if fmt == "modrinth":
        seen = {}
        for pf in files:
            key = (pf.get("path") or "").lower()
            if key in seen:
                blockers.append({"project": _name(pf.get("name")), "reason": "name_clash",
                                 "error": "its path %s is used by another file in the new pack" % untrusted.clip(pf["path"], 80)})
            seen[key] = True
    cap = _cap("max_pack_files")
    if cap is not None and len(files) > cap:
        return fail(TOO_BIG, "The new pack would hold %d files, more than the pack file limit (%d, a tool setting)." %
                    (len(files), cap))

    # allow_drop: what cannot be carried is left out instead of stopping the write.  Only that: a clash of two paths stays a
    # blocker (which of the two to keep is not for Jarvis to guess), and a pack with nothing left in it is never written.
    allow_drop = _flag(args.get("allow_drop"), False)
    drop_refused = None
    if allow_drop and uncarried:
        if any(b["reason"] != "cannot_carry" for b in blockers):
            drop_refused = "clash"
        elif not files:
            drop_refused = "empty"
        else:
            for e, blocker in uncarried:
                e["was"] = e["reason"] if e["state"] == "skipped" else None
                e["state"], e["reason"], e["error"] = "dropped", "cannot_carry", blocker["error"]
            blockers = []

    overrides, bad = _override_plan(fmt, info, path)
    if bad:
        return bad
    prepared = {
        "format": fmt, "source": path, "folder": folder, "filename": filename, "target": target, "name": pack_name,
        "version": info.get("version") or "1.0.0", "author": info.get("author") or "", "summary": info.get("summary") or "",
        "mc_version": mc, "loader": loader, "loader_version": loader_version, "same_target": same_target,
        "channel": channel, "files": files, "entries": entries, "added": added, "dependency_skipped": plan_skipped,
        "warnings": list(dict.fromkeys(plan_warnings)), "blockers": blockers, "overrides": overrides,
        "allow_drop": allow_drop, "drop_refused": drop_refused,
        "original": {"name": info.get("name"), "version": info.get("version"), "mc_version": info.get("mc_version"),
                     "loader": info.get("loader"), "loader_version": info.get("loader_version")},
        "not_copied": list(inner["others"]), "extra_index_keys": list(inner["extra_index_keys"]),
        "extra_deps": list(inner.get("extra_deps") or []), "problems": list(info.get("problems") or []),
        "pack_warnings": list(info.get("warnings") or []), "partial_names": False,
    }
    if P_warn:
        prepared["warnings"].append(P_warn)
    if loader != info.get("loader") or mc != info.get("mc_version"):
        prepared["warnings"].append("The pack's Minecraft version or loader was changed (%s %s -> %s %s); the loader version "
                                    "was %s." % (info.get("mc_version") or "?", info.get("loader") or "?", mc, loader,
                                                 "taken from the pack" if loader_version and loader_version["source"] == "pack"
                                                 else "looked up (the newest for that Minecraft version)"))
    return {"ok": True, "prepared": prepared}


def _pinned(fmt, e):
    entry = {"project": str(e["new"]["project_id"]), "file": str(e["new"]["file_id"])}
    raw = e["raw"] or {}
    if fmt == "modrinth" and raw.get("env"):
        entry["env"] = dict(raw["env"])
    if fmt == "curseforge" and raw.get("required") is False:
        entry["required"] = False
    return entry


def _override_plan(fmt, info, source):
    """The override members to copy, as the pack writer's zip source -> ({"files", "bytes", "renamed", "plan"}, None) |
    (None, failure).  The names and the caps are the writer's to check (packs.overrides_from_zip); nothing is opened here."""
    inner = info["_internal"]
    members, renamed = [], False
    for old, group, rel, size, crc in inner["override_entries"]:
        new = old if fmt == "modrinth" else packs.OVERRIDES + "/" + rel
        renamed = renamed or new != old
        members.append((old, new, size, crc))
    plan, bad = packs.overrides_from_zip(source, members, fmt)
    if bad:
        return None, bad
    return {"files": len(members), "bytes": plan["bytes"], "renamed": renamed, "plan": plan if members else None}, None


def _nothing(info, fmt, path, mc, loader, message, entries=None, inner=None):
    counts = {"entries": len(info["files"]), "unchanged": 0, "updated": 0, "skipped": 0, "dependencies_added": 0}
    out = {"ok": True, "nothing_to_update": True, "written": False, "dry_run": False, "installed": False, "format": fmt,
           "source": fmt, "original": {"path": str(path), "name": info.get("name"), "version": info.get("version")},
           "mc_version": mc, "loader": loader, "counts": counts, "updated": [], "unchanged": [], "skipped": [],
           "dependencies_added": []}
    texts = [info.get("name")]
    if entries is not None:
        for e in entries:
            if e["state"] == "unchanged":
                counts["unchanged"] += 1
                out["unchanged"].append(_line_unchanged(e))
            elif e["state"] == "skipped":
                counts["skipped"] += 1
                out["skipped"].append(_line_skipped(e))
        out["unchanged"], out["skipped"] = out["unchanged"][:REPORT_LIST_MAX], out["skipped"][:REPORT_LIST_MAX]
        for e in entries:
            texts += [_label(e), _old_name(e), e.get("error")]
    out["message"] = message or ("Nothing needed updating: every entry that could be checked already has the newest compatible "
                                 "file%s. No new pack was written and the original is untouched." %
                                 (" (%d entr%s can't be updated, see skipped)" % (counts["skipped"], "y" if counts["skipped"] == 1 else "ies")
                                  if counts["skipped"] else ""))
    return untrusted.mark(out, *texts)


# --------------------------------------------------------------------------- the report

def _line_unchanged(e):
    out = {"project": _label(e), "file_name": _old_name(e)}
    if e.get("note"):
        out["note"] = e["note"]
    return out


def _line_skipped(e):
    out = {"project": _label(e), "file_name": _old_name(e), "reason": e["reason"], "error": e["error"],
           "kept": "as it was"}
    if e.get("page_url"):
        out["page_url"] = e["page_url"]
    if e.get("cause"):
        out["cause"] = e["cause"]
    return out


def _line_dropped(e):
    out = {"project": _label(e), "file_name": _old_name(e), "reason": e["reason"], "error": e["error"],
           "kept": "no: left out of the new pack"}
    if e.get("was"):
        out["was"] = e["was"]                                   # why it had been skipped before (not_found ...)
    if e.get("page_url"):
        out["page_url"] = e["page_url"]
    return out


def _line_updated(e):
    new = e["new"]
    out = {"project": _label(e), "from": _old_name(e), "to": new.get("file_name") or "",
           "from_version": _name((e["installed"] or {}).get("version_label"), 60), "to_version": _name(new.get("version_label"), 60),
           "to_channel": new.get("channel") or "release", "published": str(new.get("date") or "")[:10]}
    if new.get("via"):
        out["via"] = new["via"]
        out["via_note"] = "a Fabric file, used because the project has no Quilt file"
    return out


def describe(prepared, dry_run, written=None):
    """The owner-facing answer for a prepared update."""
    P = prepared
    entries = P["entries"]
    unchanged = [e for e in entries if e["state"] == "unchanged"]
    updated = [e for e in entries if e["state"] == "updated"]
    skipped = [e for e in entries if e["state"] == "skipped"]
    dropped = [e for e in entries if e["state"] == "dropped"]
    ov = P["overrides"]
    lv = P.get("loader_version")
    result = {
        "ok": True, "dry_run": bool(dry_run), "written": bool(written), "installed": False, "format": P["format"],
        "source": P["format"], "original": {"path": str(P["source"]), "name": P["original"]["name"],
                                             "version": P["original"]["version"],
                                             "mc_version": P["original"]["mc_version"], "loader": P["original"]["loader"],
                                             "modified": False},
        "path": str(P["target"]), "dest": str(P["folder"]), "filename": P["filename"], "name": P["name"],
        "version": P["version"], "mc_version": P["mc_version"], "loader": P["loader"],
        "counts": {"entries": len(entries), "unchanged": len(unchanged), "updated": len(updated),
                   "skipped": len(skipped), "dependencies_added": len(P["added"]), "override_files": ov["files"]},
        "complete": not skipped and not dropped,
        "updated": [_line_updated(e) for e in updated][:REPORT_LIST_MAX],
        "unchanged": [_line_unchanged(e) for e in unchanged][:REPORT_LIST_MAX],
        "skipped": [_line_skipped(e) for e in skipped][:REPORT_LIST_MAX],
        "dependencies_added": [{"project": _name(pf.get("name")), "file_name": pf.get("file_name") or "",
                                "needed_by": _name(pf.get("dependency_of"))} for pf in P["added"]][:REPORT_LIST_MAX],
        "overrides": {"files": ov["files"], "bytes": ov["bytes"], "copied": bool(written),
                      "note": "Override files are copied from the old pack unchanged (never read, run or checked)."
                              + (" The overrides folder is written as 'overrides/'." if ov["renamed"] else "")},
    }
    if lv:
        result["loader_version"] = lv["value"]
        result["loader_version_source"] = "the pack's own" if lv["source"] == "pack" else lv["source"]
    if P.get("allow_drop"):
        # Only when it was asked for, so a run without it reports exactly what it always did.  An empty list says that
        # nothing had to be left out.
        result["allow_drop"] = True
        result["dropped"] = [_line_dropped(e) for e in dropped][:REPORT_LIST_MAX]
        result["counts"]["dropped"] = len(dropped)
    if len(updated) > REPORT_LIST_MAX or len(unchanged) > REPORT_LIST_MAX or len(skipped) > REPORT_LIST_MAX:
        result["lists_cut_at"] = REPORT_LIST_MAX
    if P["dependency_skipped"]:
        result["dependencies_not_added"] = [{"project": untrusted.clip(s.get("project"), 60), "reason": s.get("reason"),
                                             "error": s.get("error")} for s in P["dependency_skipped"]][:50]
    if P["not_copied"]:
        result["not_copied"] = {"count": len(P["not_copied"]), "sample": [untrusted.clip(n, 100) for n in P["not_copied"][:5]],
                                "note": "Entries that are neither the index nor an override (a README, an icon, a mod list) "
                                        "are not copied."}
    warnings = []
    if dropped:
        warnings.append("%d entr%s can't be carried into a pack Jarvis writes and %s LEFT OUT of the new pack because "
                        "allow_drop was set (see dropped), so it has %d fewer file%s than the old one." %
                        (len(dropped), "y" if len(dropped) == 1 else "ies", "was" if len(dropped) == 1 else "were",
                         len(dropped), "" if len(dropped) == 1 else "s"))
    warnings += list(P["warnings"])
    if P["extra_index_keys"] or P["extra_deps"]:
        warnings.append("Some fields of the old index that Jarvis doesn't read were not carried into the new pack: %s." %
                        ", ".join(untrusted.clip(k, 30) for k in (P["extra_index_keys"] + P["extra_deps"])[:6]))
    warnings += P["pack_warnings"]
    if skipped:
        warnings.append("%d entr%s could not be updated and stay%s as %s in the new pack." %
                        (len(skipped), "y" if len(skipped) == 1 else "ies", "s" if len(skipped) == 1 else "",
                         "it was" if len(skipped) == 1 else "they were"))
    if warnings:
        result["warnings"] = warnings[:20]
    if P["blockers"]:
        result["ok"] = False
        result["reason"] = VERIFY_FAILED
        result["blockers"] = P["blockers"][:50]
        result["written"] = False
        result["error"] = ("The new pack was NOT written: %d entr%s can't be carried into a pack Jarvis writes, and dropping "
                           "them would make the new pack smaller than the old one." % (
                               len(P["blockers"]), "y" if len(P["blockers"]) == 1 else "ies"))
        if P.get("drop_refused") == "clash":
            result["error"] += (" allow_drop only leaves out entries that can't be carried: the entries that would share a "
                                "path still stop the write.")
        elif P.get("drop_refused") == "empty":
            result["error"] += " allow_drop would leave nothing in the new pack, so nothing was dropped."
        elif not P.get("allow_drop") and all(b.get("reason") == "cannot_carry" for b in P["blockers"]):
            result["error"] += (" If the owner agrees to a smaller pack, run it again with allow_drop=true: the pack is then "
                                "written without them and every one is listed under dropped.")
    what = "%d updated, %d unchanged, %d skipped" % (len(updated), len(unchanged), len(skipped))
    if dropped:
        what += ", %d left out" % len(dropped)
    if P["added"]:
        what += ", %d dependenc%s added" % (len(P["added"]), "y" if len(P["added"]) == 1 else "ies")
    if not result["ok"]:
        result["message"] = result["error"]
    elif written:
        result["message"] = ("Wrote %s (%s; %d override file%s copied unchanged). The original %s was not touched. Written "
                             "only: nothing was installed or opened." % (P["target"], what, ov["files"],
                                                                         "" if ov["files"] == 1 else "s", str(P["source"])[:80]))
    else:
        result["message"] = ("Dry run: would write %s (%s; %d override file%s copied unchanged). Nothing was written or "
                             "created and the original is untouched." % (P["target"], what, ov["files"],
                                                                         "" if ov["files"] == 1 else "s"))
    texts = [P["original"]["name"], P["name"]]
    for e in entries:
        texts += [_label(e), _old_name(e), e.get("error")]
        if e["new"]:
            texts += [e["new"].get("file_name"), e["new"].get("version_label")]
    for pf in P["added"]:
        texts += [pf.get("name"), pf.get("file_name")]
    return untrusted.mark(result, *texts)


# --------------------------------------------------------------------------- the write

def _plan_for_writer(P):
    """The plan dict packs.write_pack wants: the new pack's files, and the old pack as the zip source of the overrides.
    Never an overwrite: the name is one that does not exist (_new_filename) and the writer refuses an existing one."""
    return dict(format=P["format"], name=P["name"] or "pack", version=P["version"], author=P["author"],
                summary=P["summary"], mc_version=P["mc_version"], loader=P["loader"],
                loader_version=P["loader_version"], files=P["files"], folder=P["folder"], filename=P["filename"],
                target=P["target"], overwrite=False, overrides=P["overrides"]["plan"], skipped=[], warnings=[])


def write(P):
    """Write the new pack.  -> describe(P, False, written=True) plus `size`, or a failure (nothing left behind)."""
    try:
        if P["blockers"]:
            return describe(P, False, written=False)
        return packs.write_pack(_plan_for_writer(P), describe_with=lambda _plan, dry_run, written=None:
                                describe(P, dry_run, written=written))
    except Exception as exc:  # noqa: BLE001 - this never raises
        return fail(UNSUPPORTED, "The new pack could not be written (%s). Nothing was kept." % type(exc).__name__, written=False)
