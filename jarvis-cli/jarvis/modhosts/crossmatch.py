"""Cross-platform matching for modpack files (Minecraft tools, agent S1).

WHAT IT IS FOR
  A pack still lists files from ONE platform (owner answer 9: a `.mrpack` can only point at cdn.modrinth.com, a
  CurseForge manifest only holds CurseForge ids).  This module adds one way to build the OTHER platform's pack from
  the same list: the entries are named and resolved on the platform they come from (`entries_from`), each chosen
  file is matched to the IDENTICAL file on the pack's platform by hash, and the matches are handed to the ordinary
  pack builder (packs.plan_pack) as pinned entries.  The pack builder itself is not changed; it neither knows nor
  cares that an entry was matched.

TWO DIRECTIONS
  CurseForge -> Modrinth   FREE.  A CurseForge file carries its sha1; Modrinth answers "which file has this sha1"
                           (modrinth.versions_by_hashes, verified by the probe).  Nothing is downloaded.
  Modrinth -> CurseForge   CurseForge identifies a file by its own fingerprint (a murmur2 hash of the file's bytes),
                           which can only be had by reading the bytes.  So each mod file is fetched from
                           cdn.modrinth.com, checked against Modrinth's published sha1 AND sha512, fingerprinted in
                           memory and thrown away; the fingerprints go to curseforge.fingerprint_matches.  This
                           breaks "a pack downloads no mod files", so it is EXPLICIT (the tool argument
                           `download_to_match`), REPORTED (the question lists the count and size first), CONFIRMED
                           (the tool asks the owner before the first byte, default no), bounded by the usual rules
                           (the `download_max_mb` and `deadline_seconds` settings, 0 = no limit of the owner's; a
                           defensive memory limit stays), and remembered (a fingerprint of a given sha1 never
                           changes, so the dry run and the real run download once between them).  It goes through
                           net.fetch directly rather than the Download Hub: the hub keeps what it downloads and
                           shows it as a row, and nothing here is kept.

THE RULES
  * An entry with no IDENTICAL file on the other platform is reported and skipped.  It is never swapped for a
    lookalike: a match must agree on the sha1 (and, from CurseForge, on the fingerprint), a name or a similar
    version number counts for nothing.  Two CurseForge files that both fit are `ambiguous`, never guessed.
  * A matched file must also be marked for the pack's Minecraft version (and loader, for a mod; Quilt may use a
    Fabric file under the same setting as everywhere else) on the platform it was matched to.
  * After the pack is planned, every matched file is checked once more: the file the pack builder actually chose
    must carry the sha1 that was matched (a Modrinth version's MAIN file is what a pack lists, and the matched file
    could be another file of the same version).  One that does not is dropped and reported `hash_mismatch`.
  * A CurseForge file whose author blocks third-party downloads is not matched to Modrinth (`not_distributable`):
    the block is the author's choice and is not routed around.
  * Dependencies are NOT matched: the pack builder adds them natively on the pack's own platform.

WHAT IS AND IS NOT VERIFIED (nothing here is presented as verified that was not)
  verified    Modrinth `POST /version_files` by sha1 (probe, 25 Oct 2026); CurseForge files carry `hashes` (algo 1 =
              sha1) and `fileFingerprint` (probe run 2).
  NOT SEEN    CurseForge's fingerprint route (curseforge.fingerprint_matches: the probe never called it and its
              swagger summary lists no such route), and the murmur2 variant below against a real CurseForge file
              (see cf_fingerprint).  The first is checked before any download (a cheap request with a fingerprint
              nobody has); the second by the owner's manual check in the delivery notes.

Settings read (tool settings of `minecraft_lookup`, 0 = no limit of the owner's): download_max_mb,
deadline_seconds, default_dest, quilt_fabric_fallback.

Nothing in this file opens a connection itself: every request goes through net.fetch or an adapter.  The pack
builder's private helpers (_Ctx, _resolve_projects, _merge_duplicates, _select_files) are reused on purpose, so an
entry is resolved on its own platform by EXACTLY the rules a one-platform pack uses; `_packs()` checks they are all
there and answers `unsupported` instead of failing if a later pack builder renames one.
"""
import hashlib
import os
import re
import struct

from . import (AMBIGUOUS, NO_COMPATIBLE_FILE, NOT_DISTRIBUTABLE, NOT_FOUND, OFFLINE, TOO_BIG, UNSUPPORTED,
               VENDOR_CHANGED, VERIFY_FAILED, cache, fail, net, outdir, resolve, sources, untrusted)

PLATFORMS = ("modrinth", "curseforge")

# ---- limits.  Defensive (memory / time), none of them is the owner's: those are the tool settings ------------
JAR_DEFENSIVE_MAX = 256 * 1024 * 1024        # one file is held in memory while it is hashed
JAR_TIMEOUT = 120                            # read timeout of ONE request for a file (net.fetch also ends a stalled body)
CACHE_KEY = "crossmatch:fingerprints:v1"
CACHE_TTL = 30 * 24 * 3600                   # a fingerprint is a function of the bytes: it never goes stale
CACHE_MAX = 4000                             # sha1 -> fingerprint pairs kept in the ONE cache entry
CACHE_SAVE_EVERY = 10
REPORT_MAX = 50

_FATAL = ("no_key", "bad_key", "rate_limited", "offline")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA512_RE = re.compile(r"^[0-9a-f]{128}$")
_PINNED_WARNING_RE = re.compile(r"^The file you pinned for (.+?) isn't marked for Minecraft ")

# Test seam: FETCH(vendor, url, *, want=..., max_bytes=..., timeout=...) in place of net.fetch.  It can take no host.
FETCH = None


# --------------------------------------------------------------------------- CurseForge's fingerprint
#
# MurmurHash2, 32 bit, seed 1, over the file's bytes with the whitespace bytes 0x09 0x0A 0x0D 0x20 left out, the
# length in the seed being the length AFTER that.  This is the variant CurseForge's own fingerprint uses as it is
# widely documented (it is the same function every third-party launcher computes); it was NOT checked against a
# real CurseForge file by the probe.  If it were wrong, every Modrinth entry would simply come back "not found on
# CurseForge", never a wrong match: a match must also agree on the sha1.  The manual check in the delivery notes
# settles it with one jar the owner has downloaded from CurseForge.
_M, _R, _SEED, _MASK = 0x5BD1E995, 24, 1, 0xFFFFFFFF
_WHITESPACE = bytes((9, 10, 13, 32))
_BLOCK = 4 * 1024 * 1024


class _Murmur2:
    """Streaming MurmurHash2 (32 bit).  The final length must be known first (it is part of the seed)."""

    def __init__(self, length, seed=_SEED):
        self._h = (seed ^ length) & _MASK
        self._tail = b""

    def feed(self, chunk):
        data = self._tail + chunk if self._tail else chunk
        usable = len(data) - (len(data) & 3)
        h = self._h
        if usable:
            for (k,) in struct.iter_unpack("<I", memoryview(data)[:usable]):
                k = (k * _M) & _MASK
                k ^= k >> _R
                k = (k * _M) & _MASK
                h = ((h * _M) & _MASK) ^ k
        self._h = h
        self._tail = bytes(data[usable:])

    def finish(self):
        h, tail = self._h, self._tail
        if len(tail) >= 3:
            h ^= tail[2] << 16
        if len(tail) >= 2:
            h ^= tail[1] << 8
        if len(tail) >= 1:
            h ^= tail[0]
            h = (h * _M) & _MASK
        h ^= h >> 13
        h = (h * _M) & _MASK
        h ^= h >> 15
        return h & _MASK


def _count_whitespace(block):
    return sum(block.count(bytes((w,))) for w in _WHITESPACE)


def cf_fingerprint(data):
    """CurseForge's fingerprint of `data` (bytes) -> an int, 0 to 2**32 - 1.  Pure; never raises for bytes."""
    if not isinstance(data, bytes):
        data = bytes(data)
    hasher = _Murmur2(len(data) - _count_whitespace(data))
    for start in range(0, len(data), _BLOCK):
        hasher.feed(data[start:start + _BLOCK].translate(None, _WHITESPACE))
    return hasher.finish()


def fingerprint_file(path):
    """The fingerprint and sha1 of a file on disk (for the owner's manual check; two passes, constant memory).
    -> {"ok": True, "fingerprint": int, "sha1": hex, "size": int} | {"ok": False, ...}"""
    try:
        size = whitespace = 0
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_BLOCK), b""):
                size += len(block)
                whitespace += _count_whitespace(block)
        hasher, sha1 = _Murmur2(size - whitespace), hashlib.sha1()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_BLOCK), b""):
                sha1.update(block)
                hasher.feed(block.translate(None, _WHITESPACE))
        return {"ok": True, "fingerprint": hasher.finish(), "sha1": sha1.hexdigest(), "size": size}
    except OSError as exc:
        return fail(NOT_FOUND, "That file could not be read (%s)." % type(exc).__name__)


def main(argv=None):
    """python -m jarvis.modhosts.crossmatch <file> ...   prints each file's CurseForge fingerprint and sha1."""
    import sys
    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths:
        print("usage: python -m jarvis.modhosts.crossmatch <file> [<file> ...]")
        return 2
    code = 0
    for path in paths:
        got = fingerprint_file(path)
        if got.get("ok"):
            print("%s  fingerprint %d  sha1 %s  (%d bytes)" % (path, got["fingerprint"], got["sha1"], got["size"]))
        else:
            print("%s  %s" % (path, got.get("error")))
            code = 1
    return code


# --------------------------------------------------------------------------- the fingerprint memory

def _remembered():
    got = cache.get(CACHE_KEY)
    return {k: v for k, v in got.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)} \
        if isinstance(got, dict) else {}


def _remember(additions):
    """One cache entry holds the whole sha1 -> fingerprint map, so a long pack never crowds out the cache's other
    answers (its entry count is capped; a thousand one-line entries would)."""
    if not additions:
        return
    merged = _remembered()
    merged.update(additions)
    while len(merged) > CACHE_MAX:
        merged.pop(next(iter(merged)))
    cache.put(CACHE_KEY, merged, CACHE_TTL)


# --------------------------------------------------------------------------- small helpers

def _setting(key, default=0):
    try:
        from .. import tool_settings
        return tool_settings.get("minecraft_lookup", key, default)
    except Exception:  # noqa: BLE001
        return default


def _cap_bytes(key):
    try:
        value = float(_setting(key, 0) or 0)
    except (TypeError, ValueError):
        return None
    return int(value * 1024 * 1024) if value > 0 else None


def _default_dest():
    raw = _setting("default_dest", "~/Downloads")
    return raw.strip() if isinstance(raw, str) and raw.strip() else "~/Downloads"


def _flag(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _clarify(question, **extra):
    out = {"ok": True, "needs_clarification": True, "question": question, "message": question}
    out.update(extra)
    return out


def _packs():
    """The pack builder, when it still has everything this module reuses; else None."""
    try:
        from . import packs
    except Exception:  # noqa: BLE001
        return None
    needed = ("_Ctx", "_resolve_projects", "_merge_duplicates", "_select_files", "_TimeUp", "parse_entries", "scan_overrides",
              "work_out_loader_version", "FORMATS", "CHANNELS", "EXTENSION", "VANILLA", "NAME_LEN", "VERSION_LEN")
    return packs if all(hasattr(packs, name) for name in needed) else None


def requested(args):
    """True when the call asks for cross-platform matching (`entries_from` is given)."""
    return isinstance(args, dict) and args.get("entries_from") not in (None, "")


def _fetcher():
    return FETCH or net.fetch


def _size_text(count):
    count = float(count or 0)
    if count >= 1024 * 1024 * 1024:
        return "%.1f GB" % (count / (1024 * 1024 * 1024))
    if count >= 1024 * 1024:
        return "%.1f MB" % (count / (1024 * 1024))
    return "%d KB" % max(1, int(count / 1024)) if count else "0 KB"


def _raw_entry(parsed):
    """A parsed entry (packs.parse_entries) back into the shape the tool takes."""
    out = {"project": parsed["ref"]}
    for key in ("file", "required", "env", "source", "channel"):
        if parsed.get(key) is not None:
            out[key] = parsed[key]
    return out


def _compatible(file, mc, loader, kind, fallback):
    """Is `file` marked for this Minecraft version (and, for a mod, this loader) on ITS platform?
    -> (True, via | None) | (False, sentence)."""
    versions = [str(v) for v in file.get("mc_versions") or []]
    if mc not in versions:
        return False, "it is marked for Minecraft %s, not %s" % (", ".join(versions[:3]) or "nothing", mc)
    if kind == "mod" and loader != "vanilla":
        loaders = [str(x).lower() for x in file.get("loaders") or []]
        if loader in loaders:
            return True, None
        if loader == "quilt" and fallback and "fabric" in loaders:
            return True, "fabric"
        return False, "it is marked for %s, not %s" % (", ".join(loaders[:3]) or "no loader", loader)
    return True, None


def _hash(file, name):
    value = str((file.get("hashes") or {}).get(name) or "").lower()
    return value if (_SHA1_RE if name == "sha1" else _SHA512_RE).match(value) else ""


# --------------------------------------------------------------------------- the run

def run(args, confirm_jars=None):
    """Work out the matches for one `make_modpack_file` call that has `entries_from`.

    -> {"stop": <a final result>}                    a failure, a question for the owner, or a partial result
     | {"args": <args for packs.plan_pack>, "cross": <report or None>}
    `confirm_jars(info) -> bool` is asked once, before the first mod file is downloaded (default: refused).
    Never raises."""
    try:
        return _run(args if isinstance(args, dict) else {}, confirm_jars)
    except Exception as exc:  # noqa: BLE001
        return {"stop": fail(UNSUPPORTED, "Matching entries on the other platform failed unexpectedly (%s)."
                             % type(exc).__name__)}


def _stop(result):
    return {"stop": result}


def _run(args, confirm_jars):
    packs = _packs()
    if packs is None:
        return _stop(fail(UNSUPPORTED, "Matching entries on the other platform needs the current pack builder, which "
                                       "this build doesn't have."))
    target = str(args.get("format") or "").strip().lower()
    if target not in packs.FORMATS:
        return _stop(fail(UNSUPPORTED, "format must be 'modrinth' (a .mrpack) or 'curseforge' (a .zip)."))
    origin = str(args.get("entries_from") or "").strip().lower()
    if origin not in PLATFORMS:
        return _stop(fail(UNSUPPORTED, "entries_from must be 'modrinth' or 'curseforge'."))

    # ---- cheap checks first, in plan_pack's own order and words: nothing is looked up or downloaded before them
    name = untrusted.clip(args.get("name"), packs.NAME_LEN)
    if not name:
        return _stop(_clarify("What should the pack be called?"))
    version = untrusted.clip(args.get("version"), packs.VERSION_LEN) or "1.0.0"
    if not str(args.get("mc_version") or "").strip():
        return _stop(_clarify("Which Minecraft version is the pack for (like 1.20.1 or 26.3)?"))
    mc = resolve.normalise_mc_version(args.get("mc_version"))
    if mc is None:
        return _stop(fail(UNSUPPORTED, "'%s' isn't a Minecraft version like 1.20.1 or 26.3." % str(args.get("mc_version"))[:30]))
    raw_loader = str(args.get("loader") or "").strip().lower()
    if not raw_loader:
        return _stop(_clarify("Which loader is the pack for (fabric, forge, neoforge or quilt)?"))
    loader = packs.VANILLA if raw_loader in (packs.VANILLA, "none") else resolve.normalise_loader(raw_loader)
    if loader is None:
        return _stop(fail(UNSUPPORTED, "'%s' isn't a loader Jarvis knows (fabric, forge, neoforge, quilt)." % raw_loader[:30]))
    channel = str(args.get("channel") or "release").strip().lower()
    if channel not in packs.CHANNELS:
        return _stop(fail(UNSUPPORTED, "channel must be release, beta or alpha."))
    entries, bad = packs.parse_entries(args.get("entries"))
    if bad:
        return _stop(bad)

    try:
        cap = int(float(_setting("max_pack_files", 0) or 0))
    except (TypeError, ValueError):
        cap = 0
    if cap > 0 and len(entries) > cap:                  # plan_pack's own limit, said before any lookup is made
        return _stop(fail(TOO_BIG, "%d entries is more than the pack file limit (%d, a tool setting)." % (len(entries), cap)))

    foreign = [i for i, e in enumerate(entries) if (e["source"] or origin) != target]
    if not foreign:                                     # nothing to match: the ordinary pack, exactly as before
        return {"args": _plain_args(args), "cross": None}

    folder, bad = outdir.resolve_dest(args.get("dest"), _default_dest())
    if bad:
        return _stop(bad)
    stem = re.sub(r"[\\/]+", "_", "%s-%s" % (name, version))
    filename, bad = outdir.pack_filename(args.get("filename"), stem, packs.EXTENSION[target])
    if bad:
        return _stop(bad)
    target_path = folder / filename
    if os.path.lexists(target_path):
        if not _flag(args.get("overwrite")):
            return _stop(fail("exists", "%s already exists; nothing was done. Give another filename, or overwrite=true to "
                                        "replace it." % str(target_path)[:160], path=str(target_path)))
        if outdir.is_link(target_path) or not os.path.isfile(target_path):
            return _stop(fail("bad_dest", "%s is not a plain file, so it is never replaced." % str(target_path)[:160]))
    if args.get("src") not in (None, ""):
        _scanned, bad = packs.scan_overrides(args["src"], folder)
        if bad:
            return _stop(bad)
    if loader != packs.VANILLA:
        _lv, bad = packs.work_out_loader_version(loader, mc, args.get("loader_version"))
        if bad:
            return _stop(bad)

    # ---- both platforms must be there; a missing CurseForge key is said now, not after some work
    other = "curseforge" if target == "modrinth" else "modrinth"
    adapters = {}
    for platform in (other, target):
        adapter = sources.get(platform)
        if adapter is None:
            return _stop(fail(UNSUPPORTED, "%s support is not installed here, so entries can't be matched between %s and "
                                           "%s." % ("CurseForge" if platform == "curseforge" else "Modrinth", other, target)))
        adapters[platform] = adapter
        if platform == "curseforge":
            asked = adapter.bulk_projects([])
            if isinstance(asked, dict) and not asked.get("ok"):
                return _stop(asked)

    try:
        seconds = float(_setting("deadline_seconds", 0) or 0)
    except (TypeError, ValueError):
        seconds = 0
    ctx = packs._Ctx(other, adapters[other], mc, loader, channel, seconds)
    cross = {"from": other, "to": target, "method": "sha1" if other == "curseforge" else "fingerprint",
             "foreign": len(foreign), "hits": [], "skipped": ctx.skipped, "warnings": ctx.warnings, "expect": {},
             "downloaded": 0, "bytes": 0, "remembered": 0}
    try:
        pairs, bad = packs._resolve_projects(ctx, [entries[i] for i in foreign])
        if bad:
            return _stop(bad)
        pairs, bad = packs._merge_duplicates(ctx, pairs)
        if bad:
            return _stop(bad)
        selected, bad = packs._select_files(ctx, pairs)
        if bad:
            return _stop(bad)
        if other == "curseforge":
            bad = _match_curseforge_to_modrinth(ctx, selected, adapters[target], cross)
        else:
            bad = _match_modrinth_to_curseforge(ctx, selected, adapters[target], cross, args, confirm_jars)
        if bad:
            return _stop(bad)
    except packs._TimeUp:
        return _stop({"ok": True, "partial": True, "written": False, "dry_run": _flag(args.get("dry_run")),
                      "installed": False, "format": target, "name": name,
                      "message": "Stopped at the time limit (%d s, a tool setting) while matching entries on the other "
                                 "platform. Nothing was written. Ask again (fingerprints already worked out are "
                                 "remembered), or raise the limit." % int(ctx.deadline),
                      "skipped": ctx.skipped[:REPORT_MAX]})

    if not cross["hits"] and len(foreign) == len(entries):      # a mixed list still has its native entries to build from
        return _stop(fail(NO_COMPATIBLE_FILE, "Nothing could be matched on %s: none of the %d entr%s named on %s has an "
                          "identical file there (see skipped)." % (target.capitalize(), len(foreign),
                                                                    "y" if len(foreign) == 1 else "ies", other.capitalize()),
                          skipped=ctx.skipped[:REPORT_MAX], written=False, crossmatch=_summary(cross)))
    by_ref = {}
    for hit in cross["hits"]:
        by_ref.setdefault(hit["entry"]["ref"], []).append(hit)
    rebuilt = []
    for index, parsed in enumerate(entries):
        if index not in foreign:
            rebuilt.append(_raw_entry(parsed))
            continue
        waiting = by_ref.get(parsed["ref"])
        if not waiting:
            continue                                    # already reported in skipped
        hit = waiting.pop(0)
        raw = {"project": hit["to_project_id"], "file": hit["to_file_id"], "source": target}
        for key in ("required", "env", "channel"):
            if parsed.get(key) is not None:
                raw[key] = parsed[key]
        rebuilt.append(raw)
    new_args = _plain_args(args)
    new_args["entries"] = rebuilt
    return {"args": new_args, "cross": cross}


def _plain_args(args):
    out = dict(args)
    out.pop("entries_from", None)
    out.pop("download_to_match", None)
    return out


# --------------------------------------------------------------------------- CurseForge -> Modrinth (no download)

def _match_curseforge_to_modrinth(ctx, selected, modrinth, cross):
    """Match each chosen CurseForge file to the Modrinth file with the same sha1.  -> None | a failure to stop with."""
    finder = getattr(modrinth, "versions_by_hashes", None)
    if not callable(finder):
        return fail(UNSUPPORTED, "Looking files up by hash isn't available on Modrinth in this build.")
    asking = []
    for item in selected:
        project, file, ref = item["project"], item["file"], item["entry"]["ref"]
        if project.get("distributable") is False or file.get("why") in ("no_url", "unavailable"):
            ctx.skip(ref, NOT_DISTRIBUTABLE, "the author doesn't allow third-party downloads of it on CurseForge, so it "
                     "isn't matched to another site", project=project, stage="match")
            continue
        if not _hash(file, "sha1"):
            ctx.skip(ref, "no_hash", "CurseForge gave no sha1 for this file, so it can't be looked up on Modrinth",
                     project=project, stage="match")
            continue
        asking.append(item)
    if not asking:
        return None
    got = finder([_hash(i["file"], "sha1") for i in asking])
    if not isinstance(got, dict) or not got.get("ok"):
        return got if isinstance(got, dict) else fail(VENDOR_CHANGED, "Modrinth's hash lookup gave no answer.")
    found, primary = got.get("files") or {}, got.get("primary") or {}
    for item in asking:
        project, file, ref = item["project"], item["file"], item["entry"]["ref"]
        sha1 = _hash(file, "sha1")
        record = found.get(sha1)
        if not record:
            ctx.skip(ref, NOT_FOUND, "Modrinth has no file with the same sha1 (it isn't there, or the build there "
                     "differs)", project=project, stage="match")
        elif primary.get(sha1) is False:
            ctx.skip(ref, "not_main_file", "the identical file is a secondary file of its Modrinth version, and a pack "
                     "lists a version's main file", project=project, stage="match")
        else:
            ok, why = _compatible(record, ctx.mc, ctx.loader, project.get("kind") or "mod", ctx.fallback)
            if not ok:
                ctx.skip(ref, NO_COMPATIBLE_FILE, "the identical file is on Modrinth, but %s there" % why,
                         project=project, stage="match")
                continue
            _hit(cross, item, record, sha1, why or file.get("via"))
    return None


def _hit(cross, item, record, sha1, via):
    hit = {"entry": item["entry"], "project": item["project"], "from_file": item["file"],
           "to_project_id": str(record["project_id"]), "to_file_id": str(record["file_id"]), "sha1": sha1}
    if via:
        hit["via"] = via
    cross["hits"].append(hit)
    cross["expect"]["%s:%s" % (hit["to_project_id"], hit["to_file_id"])] = sha1


# --------------------------------------------------------------------------- Modrinth -> CurseForge (fingerprints)

def _match_modrinth_to_curseforge(ctx, selected, curseforge, cross, args, confirm_jars):
    """Fingerprint each chosen Modrinth file (downloading it only when its fingerprint isn't remembered) and look the
    fingerprints up on CurseForge.  -> None | a failure / question to stop with."""
    finder = getattr(curseforge, "fingerprint_matches", None)
    if not callable(finder):
        return fail(UNSUPPORTED, "Looking files up by fingerprint isn't available on CurseForge in this build.")
    known = _remembered()
    cap = _cap_bytes("download_max_mb")
    limit = min(cap, JAR_DEFENSIVE_MAX) if cap else JAR_DEFENSIVE_MAX
    prints, todo = {}, []                                # id(item) -> fingerprint ; items that need their file
    for item in selected:
        project, file, ref = item["project"], item["file"], item["entry"]["ref"]
        sha1, sha512 = _hash(file, "sha1"), _hash(file, "sha512")
        if not (sha1 and sha512):
            ctx.skip(ref, "no_hash", "Modrinth gave no sha1 and sha512 for this file, so a download couldn't be checked",
                     project=project, stage="match")
        elif sha1 in known:
            prints[id(item)] = known[sha1]
            cross["remembered"] += 1
        elif not file.get("download_url"):
            ctx.skip(ref, "bad_download_url", "it has no download link on cdn.modrinth.com", project=project, stage="match")
        elif isinstance(file.get("size"), int) and file["size"] > limit:
            why = ("it is %s, over the download limit (%s, a tool setting)" % (_size_text(file["size"]), _size_text(cap))
                   if cap and file["size"] > cap else
                   "it is %s, more than Jarvis will hold in memory to fingerprint it" % _size_text(file["size"]))
            ctx.skip(ref, TOO_BIG, why, project=project, stage="match")
        else:
            todo.append(item)

    if todo:
        total = sum(i["file"]["size"] for i in todo if isinstance(i["file"].get("size"), int))
        info = {"count": len(todo), "bytes": total, "remembered": cross["remembered"],
                "files": [{"project": i["project"].get("name") or i["entry"]["ref"], "file_name": i["file"].get("file_name") or "",
                           "size": i["file"].get("size")} for i in todo[:REPORT_MAX]]}
        if not _flag(args.get("download_to_match")):
            return _clarify(
                "To put %d Modrinth entr%s in a CurseForge pack, Jarvis has to download %d mod file%s from Modrinth "
                "(about %s), work out CurseForge's fingerprint of each and look it up on CurseForge. Nothing is saved: each "
                "file is read in memory and discarded, and the fingerprints are remembered so a second run downloads "
                "nothing. Ask the owner; if they agree, call again with download_to_match=true." % (
                    len(selected), "y" if len(selected) == 1 else "ies", len(todo), "" if len(todo) == 1 else "s",
                    _size_text(total) if total else "unknown size"),
                needs_download=True, download_count=len(todo), download_bytes=total, from_platform="modrinth",
                to_platform="curseforge", written=False, installed=False, download_files=info["files"])
        probe = finder([getattr(curseforge, "FINGERPRINT_PROBE", 1)])    # does the route answer?  BEFORE any download
        if not isinstance(probe, dict) or not probe.get("ok"):
            return probe if isinstance(probe, dict) else fail(VENDOR_CHANGED, "CurseForge's fingerprint lookup gave no answer.")
        if not (callable(confirm_jars) and confirm_jars(info)):
            return fail(UNSUPPORTED, "Not confirmed: no mod file was downloaded and nothing was written.", cancelled=True,
                        written=False)
        bad = _fingerprint_downloads(ctx, todo, prints, cross, limit)
        if bad:
            return bad

    ready = [item for item in selected if id(item) in prints]
    if not ready:
        return None
    got = finder(sorted({prints[id(i)] for i in ready}))
    if not isinstance(got, dict) or not got.get("ok"):
        return got if isinstance(got, dict) else fail(VENDOR_CHANGED, "CurseForge's fingerprint lookup gave no answer.")
    matches = got.get("matches") or {}
    for item in ready:
        project, file, ref = item["project"], item["file"], item["entry"]["ref"]
        print_, sha1 = prints[id(item)], _hash(item["file"], "sha1")
        candidates, differing = {}, 0
        for record in matches.get(print_) or []:
            theirs = _hash(record, "sha1")
            stamp = str((record.get("hashes") or {}).get("fingerprint") or "")
            if stamp and stamp != str(print_):
                continue
            if theirs == sha1:
                candidates[(record["project_id"], record["file_id"])] = record
            else:
                differing += 1
        if not candidates:
            ctx.skip(ref, NOT_FOUND, "CurseForge has no file identical to it" + (
                " (it has one with the same fingerprint but a different sha1, which is not used)" if differing else ""),
                project=project, stage="match")
        elif len(candidates) > 1:
            ctx.skip(ref, AMBIGUOUS, "more than one CurseForge file is identical to it; Jarvis doesn't pick between them",
                     project=project, stage="match",
                     candidates=[{"project_id": p, "file_id": f, "file_name": r.get("file_name") or ""}
                                 for (p, f), r in sorted(candidates.items())][:5])
        else:
            record = next(iter(candidates.values()))
            ok, why = _compatible(record, ctx.mc, ctx.loader, project.get("kind") or "mod", ctx.fallback)
            if not ok:
                ctx.skip(ref, NO_COMPATIBLE_FILE, "the identical file is on CurseForge, but %s there" % why,
                         project=project, stage="match")
                continue
            _hit(cross, item, record, sha1, why or file.get("via"))
    return None


def _fingerprint_downloads(ctx, todo, prints, cross, limit):
    """Download, verify and fingerprint each file of `todo`, one at a time.  -> None | a failure to stop with (what was
    done so far is kept either way)."""
    fresh = {}
    stop = None
    try:
        for item in todo:
            ctx.tick()
            file, project, ref = item["file"], item["project"], item["entry"]["ref"]
            reply = _fetcher()("modrinth", file["download_url"], want="bytes", max_bytes=limit, timeout=JAR_TIMEOUT)
            if not isinstance(reply, dict) or not reply.get("ok"):
                reason = reply.get("reason") if isinstance(reply, dict) else OFFLINE
                if reason in _FATAL:
                    stop = reply if isinstance(reply, dict) else fail(OFFLINE, "Modrinth did not answer.")
                    break
                ctx.skip(ref, reason if reason in (NOT_FOUND, TOO_BIG) else VENDOR_CHANGED,
                         untrusted.clip((reply or {}).get("error") if isinstance(reply, dict) else "", 160) or
                         "the file could not be downloaded", project=project, stage="match")
                continue
            body = reply.get("body")
            if not isinstance(body, (bytes, bytearray)):
                ctx.skip(ref, VENDOR_CHANGED, "Modrinth did not send a file", project=project, stage="match")
                continue
            body = bytes(body)
            size = file.get("size")
            if (isinstance(size, int) and size != len(body)) or hashlib.sha1(body).hexdigest() != _hash(file, "sha1") \
                    or hashlib.sha512(body).hexdigest() != _hash(file, "sha512"):
                ctx.skip(ref, VERIFY_FAILED, "the downloaded file does not match Modrinth's own size and hashes, so it "
                         "was not used", project=project, stage="match")
                continue
            fingerprint = cf_fingerprint(body)
            cross["downloaded"] += 1
            cross["bytes"] += len(body)
            fresh[_hash(file, "sha1")] = fingerprint
            prints[id(item)] = fingerprint
            if len(fresh) % CACHE_SAVE_EVERY == 0:
                _remember(fresh)
                fresh = {}
    finally:
        _remember(fresh)                                  # a time limit or an error loses no finished work
    return stop


# --------------------------------------------------------------------------- after the pack is planned

def fold_failure(result, cross):
    """plan_pack refused: its `skipped` list gets the matching stage's entries in front."""
    if not isinstance(result, dict) or not cross:
        return result
    out = dict(result)
    if isinstance(out.get("skipped"), list) or not out.get("ok"):
        out["skipped"] = (list(cross["skipped"]) + list(out.get("skipped") or []))[:REPORT_MAX]
    out["crossmatch"] = _summary(cross)
    return out


def apply(plan, cross):
    """Check the planned files against what was matched and merge the reports into `plan` (in place).
    -> None, or a failure when nothing is left to put in the pack."""
    listed, dropped = [], []
    for pf in plan["files"]:
        want = cross["expect"].get("%s:%s" % (pf["project_id"], pf["file_id"]))
        have = str((pf.get("hashes") or {}).get("sha1") or "").lower()
        if want is None or have == want:
            listed.append(pf)
        else:
            dropped.append({"project": untrusted.clip(pf.get("name"), 100), "name": untrusted.clip(pf.get("name"), 100),
                            "reason": "hash_mismatch",
                            "error": "the file the pack would list is not the one that was matched (its sha1 differs), "
                                     "so it was left out", "stage": "match"})
    kept = {"%s:%s" % (pf["project_id"], pf["file_id"]) for pf in listed}
    cross["hits"] = [h for h in cross["hits"] if "%s:%s" % (h["to_project_id"], h["to_file_id"]) in kept]
    via_names = {str(h["project"].get("name") or "") for h in cross["hits"] if h.get("via")}
    plan["files"] = listed
    plan["skipped"] = list(cross["skipped"]) + list(plan["skipped"]) + dropped
    plan["warnings"] = [w for w in cross["warnings"] + list(plan["warnings"])
                        if not (_PINNED_WARNING_RE.match(w or "") and _PINNED_WARNING_RE.match(w).group(1) in via_names)]
    overrides = plan.get("overrides")
    if not listed and not (overrides and overrides["files"]):
        return fail(NO_COMPATIBLE_FILE, "Nothing could be put in the pack: every matched file was left out (see skipped).",
                    skipped=plan["skipped"][:REPORT_MAX], written=False, crossmatch=_summary(cross))
    return None


def _summary(cross):
    out = {"from": cross["from"], "to": cross["to"], "method": cross["method"], "entries": cross["foreign"],
           "matched": len(cross["hits"]), "not_matched": max(0, cross["foreign"] - len(cross["hits"])),
           "downloaded_files": cross["downloaded"], "downloaded_bytes": cross["bytes"], "remembered": cross["remembered"]}
    if cross["method"] == "fingerprint":
        out["note"] = "Mod files were read in memory to work out CurseForge's fingerprint and were not saved."
    return out


def annotate(result, cross):
    """Add the matching report to a describe() result.  Returns it."""
    if not isinstance(result, dict) or not cross:
        return result
    summary = _summary(cross)
    result["crossmatch"] = summary
    matched = []
    for hit in cross["hits"]:
        item = {"project": hit["project"].get("name") or hit["entry"]["ref"], "from_file_id": str(hit["from_file"].get("file_id") or ""),
                "project_id": hit["to_project_id"], "file_id": hit["to_file_id"]}
        if hit.get("via"):
            item["via"] = hit["via"]
            item["via_note"] = "a Fabric file, offered because the project has no Quilt file"
        matched.append(item)
    result["cross_matched"] = matched[:REPORT_MAX]
    shown = {"modrinth": "Modrinth", "curseforge": "CurseForge"}
    sentence = " %d of %d entr%s named on %s %s matched on %s by %s." % (
        summary["matched"], summary["entries"], "y" if summary["entries"] == 1 else "ies", shown[cross["from"]],
        "was" if summary["entries"] == 1 else "were", shown[cross["to"]], summary["method"])
    if summary["downloaded_files"]:
        sentence += " %d mod file%s (%s) read to fingerprint, not saved." % (
            summary["downloaded_files"], "" if summary["downloaded_files"] == 1 else "s", _size_text(summary["downloaded_bytes"]))
    if isinstance(result.get("message"), str):
        result["message"] += sentence
    texts = [m["project"] for m in matched]
    if any(untrusted.looks_like_instruction(t) for t in texts):
        untrusted.mark(result, *texts)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
