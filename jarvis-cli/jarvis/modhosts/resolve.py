"""Choosing a file, naming a project, closing dependencies, and reading loader / Minecraft version text.

One place for the rules of plan section 11 (tests/test_modhosts_resolve.py is a table over them):

  * A file matches only when its Minecraft version equals `mc_version` EXACTLY (strings, never ranges) and
    its loader matches.  Loaders are strict: NeoForge and Forge are different loaders.
  * Quilt falls back to Fabric (owner answer 10, setting `quilt_fabric_fallback`, default true): a project
    with no Quilt file offers its Fabric file, marked `via: "fabric"`.  The caller must fetch the files with
    the loader filter OFF in that case (Quilt queries never return Fabric files) and let pick_file choose.
  * `channel` is the least stable channel the owner accepts: release (default) accepts releases only, beta
    accepts releases and betas, alpha accepts everything.  Inside what is accepted, release is preferred to
    beta to alpha, and the newest date wins.  A `pinned` file id always wins.
  * With no `mc_version` or no `loader` the result is flagged `unfiltered`.
  * `no_compatible_file` lists the closest alternatives (nearest Minecraft versions) instead of a bare error.
  * resolve_project follows the never-guess rule (D10): an exact id or slug wins; otherwise the one result
    whose normalised name equals the query and is the only such result; otherwise the candidates are returned.

Every function returns a plain dict (or a plain value for the two normalisers) and never raises.
"""
import re
from datetime import datetime

from . import AMBIGUOUS, NO_COMPATIBLE_FILE, NOT_FOUND, UNSUPPORTED, VENDOR_CHANGED, fail, sources

CHANNELS = ("release", "beta", "alpha")
_CHANNEL_RANK = {"release": 0, "beta": 1, "alpha": 2}
_ACCEPTED = {"release": ("release",), "beta": ("release", "beta"), "alpha": ("release", "beta", "alpha")}
ALTERNATIVES = 5                       # how many closest alternatives a no_compatible_file answer lists
NAME_CANDIDATES = 5

_LOADER_ALIASES = {
    "fabric": "fabric", "fabricmc": "fabric", "fabric-loader": "fabric", "fabric loader": "fabric",
    "forge": "forge", "minecraftforge": "forge", "minecraft forge": "forge", "forge-loader": "forge",
    "neoforge": "neoforge", "neo-forge": "neoforge", "neo forge": "neoforge", "neoforged": "neoforge",
    "neoforge-loader": "neoforge",
    "quilt": "quilt", "quiltmc": "quilt", "quilt-loader": "quilt", "quilt loader": "quilt",
}
# Digits and dots, two or three parts: 1.21.11, 26.3, 26.1.2.  Never assume a leading "1.".
_MC_RE = re.compile(r"^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})(?:\.(0|[1-9][0-9]{0,2}))?$")
_MC_PREFIX_RE = re.compile(r"^(?:minecraft|mc|v)[\s:_-]*", re.I)
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,63}$")


# --------------------------------------------------------------------------- loader / version text

def normalise_loader(text):
    """'Fabric', 'neo forge', 'quilt-loader' -> 'fabric' | 'forge' | 'neoforge' | 'quilt'; anything else None."""
    if not isinstance(text, str):
        return None
    return _LOADER_ALIASES.get(" ".join(text.strip().lower().split()))


def normalise_mc_version(text):
    """'1.21.11', 'v26.3', 'Minecraft 26.1.2' -> '1.21.11' / '26.3' / '26.1.2'; None when it is not a release
    version of two or three numeric parts (snapshots, ranges, 'latest' and wild cards are not versions)."""
    if not isinstance(text, str):
        return None
    value = _MC_PREFIX_RE.sub("", text.strip())
    return value if _MC_RE.match(value) else None


def version_key(version):
    """A sortable tuple of three ints for a version string (missing parts count as 0); (0, 0, 0) for junk."""
    try:
        parts = [int(p) for p in str(version).split(".")][:3]
    except (TypeError, ValueError):
        return (0, 0, 0)
    return tuple(parts + [0] * (3 - len(parts)))


def _distance(a, b):
    ka, kb = version_key(a), version_key(b)
    return abs(ka[0] - kb[0]) * 1_000_000 + abs(ka[1] - kb[1]) * 1_000 + abs(ka[2] - kb[2])


# --------------------------------------------------------------------------- picking a file

def _date_key(file):
    text = str((file or {}).get("date") or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _channel_of(file):
    channel = str((file or {}).get("channel") or "release").lower()
    return channel if channel in _CHANNEL_RANK else "alpha"


def _quilt_fallback_setting(value):
    if value is not None:
        return bool(value)
    try:
        from .. import tool_settings
        return bool(tool_settings.get("minecraft_lookup", "quilt_fabric_fallback", default=True))
    except Exception:  # noqa: BLE001
        return True


def _loaders_of(file):
    return [str(x).lower() for x in (file or {}).get("loaders") or []]


def _with_via(file):
    out = dict(file)
    out["via"] = "fabric"
    return out


def _mc_ok(file, mc_version):
    return mc_version is None or mc_version in [str(v) for v in (file or {}).get("mc_versions") or []]


def _usable(files):
    out = []
    for f in files or []:
        if isinstance(f, dict) and not f.get("is_server_pack"):
            out.append(f)
    return out


def _best(files):
    return min(files, key=lambda f: (_CHANNEL_RANK[_channel_of(f)], -_date_key(f)))


def _native_and_fallback(files, mc_version, loader, fallback, accepted):
    native, via = [], []
    for f in files:
        if not _mc_ok(f, mc_version) or _channel_of(f) not in accepted:
            continue
        loaders = _loaders_of(f)
        if loader is None or loader in loaders:
            native.append(f)
        elif loader == "quilt" and fallback and "fabric" in loaders:
            via.append(_with_via(f))
    return native, via


def filter_files(files, mc_version=None, loader=None, channel="release", quilt_fallback=None):
    """The compatible files, newest first (an extra function the lookup tool lists from).  Quilt: native Quilt
    files, or, when there are none, the Fabric ones marked `via`."""
    try:
        accepted = _ACCEPTED.get(str(channel or "release").lower(), _ACCEPTED["release"])
        native, via = _native_and_fallback(_usable(files), mc_version, loader, _quilt_fallback_setting(quilt_fallback),
                                           accepted)
        chosen = native or via
        return sorted(chosen, key=lambda f: -_date_key(f))
    except Exception:  # noqa: BLE001
        return []


def pick_file(files, mc_version=None, loader=None, channel="release", pinned=None, quilt_fallback=None):
    """-> {"ok": True, "file": file, "unfiltered": bool}
       | {"ok": False, "reason": "no_compatible_file", "error": ..., "alternatives": [file]}"""
    try:
        files = _usable(files)
        unfiltered = mc_version is None or loader is None
        if pinned not in (None, ""):
            for f in files:
                if str(f.get("file_id")) == str(pinned):
                    return {"ok": True, "file": f, "unfiltered": unfiltered}
            return fail(NO_COMPATIBLE_FILE, "That file is not among the project's files.", alternatives=[])
        fallback = _quilt_fallback_setting(quilt_fallback)
        accepted = _ACCEPTED.get(str(channel or "release").lower(), _ACCEPTED["release"])
        native, via = _native_and_fallback(files, mc_version, loader, fallback, accepted)
        chosen = native or via
        if chosen:
            return {"ok": True, "file": _best(chosen), "unfiltered": unfiltered}
        return _none_found(files, mc_version, loader, fallback, accepted)
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The file list could not be read.")


def _none_found(files, mc_version, loader, fallback, accepted):
    scored = []
    for f in files:
        loaders = _loaders_of(f)
        if loader is None or loader in loaders:
            mismatch = 0
        elif loader == "quilt" and fallback and "fabric" in loaders:
            mismatch = 0
        else:
            mismatch = 1
        mc = [str(v) for v in f.get("mc_versions") or []]
        dist = min((_distance(v, mc_version) for v in mc), default=10 ** 9) if mc_version else 0
        scored.append((mismatch, dist, _CHANNEL_RANK[_channel_of(f)], -_date_key(f), f))
    scored.sort(key=lambda t: t[:4])
    out, seen = [], set()
    for _m, _d, _c, _t, f in scored:
        marker = str(f.get("file_id"))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(_with_via(f) if (loader == "quilt" and fallback and "quilt" not in _loaders_of(f)
                                    and "fabric" in _loaders_of(f)) else f)
        if len(out) >= ALTERNATIVES:
            break
    what = []
    if mc_version:
        what.append("Minecraft %s" % mc_version)
    if loader:
        what.append(loader)
    wrong_channel = any(
        _mc_ok(f, mc_version) and (loader is None or loader in _loaders_of(f)) and _channel_of(f) not in accepted
        for f in files)
    text = "No compatible file%s." % (" for " + " on ".join(what) if what else "")
    if wrong_channel:
        text += " There are files for it in a less stable channel (beta or alpha): ask for that channel to see them."
    elif out:
        text += " The closest alternatives are listed."
    return fail(NO_COMPATIBLE_FILE, text, alternatives=out)


# --------------------------------------------------------------------------- naming a project (never guess)

def _norm_name(text):
    return re.sub(r"[^0-9a-z]+", "", str(text or "").lower())


def resolve_project(source, ref, kind=None):
    """-> {"ok": True, "project": project}
       | {"ok": False, "reason": "ambiguous" | "not_found", "candidates": [project]} (or another failure, as is)"""
    try:
        adapter = sources.get(source)
        if adapter is None:
            if str(source).lower() == "curseforge":
                return fail(UNSUPPORTED, "CurseForge support is not installed yet.", candidates=[])
            return fail(UNSUPPORTED, "Unknown source %r: use modrinth or curseforge." % (source,), candidates=[])
        text = str(ref if ref is not None else "").strip()
        if not text:
            return fail(NOT_FOUND, "No project was named.", candidates=[])
        if _REF_RE.match(text):
            found = adapter.get_project(text)
            if found.get("ok"):
                return {"ok": True, "project": found["project"]}
            if found.get("reason") != NOT_FOUND:
                return found
        searched = adapter.search(text, kind=kind or "mod", limit=10)
        if not searched.get("ok"):
            return searched
        hits = searched.get("hits") or []
        if not hits:
            return fail(NOT_FOUND, "Nothing matches %r." % text[:60], candidates=[])
        wanted = _norm_name(text)
        exact = [h for h in hits if wanted and _norm_name(h.get("name")) == wanted]
        if len(exact) == 1:
            return {"ok": True, "project": exact[0]}
        return fail(AMBIGUOUS, "Several projects could match %r; say which one." % text[:60],
                    candidates=hits[:NAME_CANDIDATES])
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The project could not be looked up.", candidates=[])


# --------------------------------------------------------------------------- dependencies

def add_dependencies(source, files, mc_version=None, loader=None, max_depth=5):
    """Close `files` over their REQUIRED dependencies.  Deduped by project, cycles broken, depth capped.

    -> {"ok": True, "added": [file], "skipped": [{"project_id": str, "reason": str}]}
    `added` holds only the new files (never the ones passed in).  A dependency that cannot be placed is
    `skipped` with a reason (a reason code, or "depth_limit"); the rest still come back."""
    try:
        adapter = sources.get(source)
        if adapter is None:
            return fail(UNSUPPORTED, "That source is not available here.")
        fallback = _quilt_fallback_setting(None)
        fetch_loader = None if (loader == "quilt" and fallback) else loader
        present = {str(f.get("project_id")) for f in files or [] if f.get("project_id") not in (None, "")}
        asked, added, skipped, skipped_ids = set(), [], [], set()

        def skip(pid, reason):
            if pid not in skipped_ids:
                skipped_ids.add(pid)
                skipped.append({"project_id": pid, "reason": reason})

        frontier = [f for f in files or [] if isinstance(f, dict)]
        depth = 0
        while frontier:
            depth += 1
            wanted = []
            for f in frontier:
                for dep in f.get("dependencies") or []:
                    if not isinstance(dep, dict) or dep.get("type") != "required":
                        continue
                    pid = str(dep.get("project_id") or "")
                    fid = dep.get("file_id")
                    if (pid and (pid in present or pid in skipped_ids)) or not (pid or fid):
                        continue
                    marker = pid or "file:%s" % fid
                    if marker in asked:
                        continue
                    asked.add(marker)
                    wanted.append((pid, str(fid) if fid not in (None, "") else None))
            if not wanted:
                break
            if depth > max_depth:
                for pid, fid in wanted:
                    skip(pid or str(fid), "depth_limit")
                break
            frontier = []
            fresh = []

            pinned = [(p, f) for p, f in wanted if f]
            if pinned:
                got = adapter.bulk_get_files(pinned)
                for p, f in pinned:
                    entry = (got.get("files") or {}).get("%s:%s" % (p, f)) if got.get("ok") else None
                    if entry:
                        fresh.append(entry)
                    else:
                        why = NOT_FOUND if got.get("ok") else (got.get("reason") or VENDOR_CHANGED)
                        skip(p or f, why)
            free = [p for p, f in wanted if p and not f]
            if free:
                listed = adapter.bulk_list_files(free, mc_version=mc_version, loader=fetch_loader)
                for pid in free:
                    if not listed.get("ok"):
                        skip(pid, listed.get("reason") or VENDOR_CHANGED)
                        continue
                    picked = pick_file((listed.get("files") or {}).get(pid) or [], mc_version, loader, channel="beta",
                                       quilt_fallback=fallback)
                    if picked.get("ok"):
                        fresh.append(picked["file"])
                    else:
                        skip(pid, picked.get("reason") or NO_COMPATIBLE_FILE)
            for entry in fresh:
                pid = str(entry.get("project_id") or "")
                if pid and pid in present:
                    continue
                if pid:
                    present.add(pid)
                added.append(entry)
                frontier.append(entry)
        return {"ok": True, "added": added, "skipped": skipped}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The dependencies could not be worked out.")
