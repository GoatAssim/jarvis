"""CurseForge source adapter (``https://api.curseforge.com/v1``, Minecraft game id 432).

Same module-level functions as ``modrinth.py`` (the interface contract of the Minecraft mod
tools): ``search``, ``get_project``, ``list_files``, ``get_file``, ``bulk_projects``,
``bulk_list_files``, ``bulk_get_files``. Every function returns a plain dict and NEVER raises;
a failure is ``{"ok": False, "error": "<one plain sentence>", "reason": "<code>"}``.

Every function takes one extra trailing keyword-only argument, ``fetch=None``. ``None`` means
"use ``jarvis.modhosts.net.fetch``" (imported at call time, so this file loads and is testable
before ``net.py`` exists); a test passes a fake with the contract's signature::

    fetch(vendor, url, *, method="GET", params=None, json_body=None, headers=None,
          want="json", max_bytes=None, max_redirects=3)
        -> {"ok": True, "status": int, "headers": {...}, "body": json, "final_url": str} | failure dict

This module never opens a connection and never sees the key's value: ``net.fetch`` adds the
``x-api-key`` header (from ``config.get_key()``) for vendor ``curseforge`` on ``api.curseforge.com``
only. The only thing asked of ``config`` here is "is there a usable key?" (so a missing key is
answered with the plain how-to-get-one sentence before any request is made).

What the owner's probe verified (25 Oct 2026, run 2, real answers; tests/data/modhosts/ and
probe/modhosts-checks-report-cf.txt) and what it did not:

VERIFIED  class ids (mods 6, modpacks 4471, resource packs 12, shaders 6552, data packs 6945);
          ModLoaderType numbers (Forge 1, Fabric 4, Quilt 5, NeoForge 6); FileReleaseType
          (1 release, 2 beta, 3 alpha); FileRelationType (3 = required); hash algo 1 = SHA-1 and
          2 = MD5 (nothing else, 50 of 50 files); ``gameVersions`` mixes Minecraft versions, loader
          names and tags such as "Client"; the loader filter is NOT exact (a Fabric file that also
          runs on Quilt is tagged with both names, so ``modLoaderType=4`` returned Quilt-tagged
          files and ``=5`` returned Fabric-tagged ones); ``allowModDistribution`` is a boolean on
          the mod; a project with it false had ``downloadUrl: null`` on all its files and the
          download-url route answered HTTP 403 with an empty body; bulk ``POST /mods`` and
          ``POST /mods/files``; search filters ``gameVersion`` / ``modLoaderType`` / ``classId``;
          ``sortField=2``; the CDN host of every real download URL was ``edge.forgecdn.net``.
NOT SEEN  a download from the CDN (the probe's two first-byte requests got 404; that may be its
          request style or gone files), so this file never builds a CDN URL: it passes on the
          ``downloadUrl`` the API gave, as is. The slug route of ``get_project`` (the ``slug``
          search parameter exists in the swagger but was never run), the sort numbers other than 2
          (taken from the swagger enum), and the ``latestFilesIndexes`` semantics (not used for
          picking files, see ``bulk_list_files``).

Vendor text (names, summaries, authors, file labels) is written by strangers and ends up in the
model's context, so it is stripped of HTML and markdown links, flattened to one line and clipped
here (the plan's D8); a file NAME is passed on as the vendor wrote it (control characters out).
"""

import re

from . import config

SOURCE = "curseforge"
VENDOR = "curseforge"
API_BASE = "https://api.curseforge.com/v1"
API_HOST = "api.curseforge.com"
GAME_ID = 432

# --- numbers from the probe / swagger (never from memory; see the module docstring) ---------
CLASS_IDS = {"mod": 6, "modpack": 4471, "resourcepack": 12, "shader": 6552, "datapack": 6945}
KIND_OF_CLASS = {v: k for k, v in CLASS_IDS.items()}
# Page slugs of those classes (curseforge_classes.json), only used to build a project page link
# when the API gave none.
CLASS_PAGE_SLUG = {"mod": "mc-mods", "modpack": "modpacks", "resourcepack": "texture-packs",
                   "shader": "shaders", "datapack": "data-packs"}
LOADER_TYPES = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
LOADER_OF_TYPE = {v: k for k, v in LOADER_TYPES.items()}
LOADER_ORDER = ("fabric", "forge", "neoforge", "quilt")
CHANNELS = {1: "release", 2: "beta", 3: "alpha"}
HASH_SHA1, HASH_MD5 = 1, 2
REL_EMBEDDED, REL_OPTIONAL, REL_REQUIRED, REL_TOOL, REL_INCOMPATIBLE, REL_INCLUDE = 1, 2, 3, 4, 5, 6
# fileStatus values that mean "this file is not for use" (swagger FileStatus: 5 Rejected,
# 6 MalwareDetected, 7 Deleted).
BAD_FILE_STATUS = (5, 6, 7)
# `sort` -> ModsSearchSortField. 2 (Popularity) is the only one the probe ran, so "relevance"
# uses it; 13 (Relevancy) exists in the swagger but was never exercised. 6, 3 and 11 are the
# swagger's TotalDownloads, LastUpdated and ReleasedDate: NOT run live (listed in Open points).
SORT_FIELDS = {"relevance": 2, "downloads": 6, "updated": 3, "newest": 11}

# Defensive parsing limits (they protect memory; they are not the owner's limits).
SEARCH_PAGE_MAX = 50
FILES_PAGE_SIZE = 50
FILES_MAX_PAGES = 40
BULK_CHUNK = 50
MAX_QUERY_CHARS = 100

_REASONS = frozenset({
    "no_key", "bad_key", "rate_limited", "not_found", "ambiguous", "not_distributable",
    "no_compatible_file", "no_loader_version", "bad_dest", "exists", "too_big", "verify_failed",
    "vendor_changed", "offline", "unsupported"})
# A failure that stops a whole batch (a missing page for one project does not).
_FATAL = frozenset({"no_key", "bad_key", "rate_limited", "offline", "vendor_changed"})

_MC_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")           # 1.21.11, 26.3, 26.1.2 (never assumes "1.")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,99}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"<[^>]*>")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_WS_RE = re.compile(r"\s+")
_ELLIPSIS = "\u2026"


# ============================================================================================
# small helpers
# ============================================================================================
def _failure(reason, error, **extra):
    out = {"ok": False, "error": error, "reason": reason}
    out.update(extra)
    return out


def _scrub(text, limit=300):
    """An error text with the key (if it ever got in) taken out. Belt and braces: net.py
    already redacts, and nothing here puts the key anywhere."""
    s = _CTRL_RE.sub(" ", str(text))
    try:
        key = config.get_key()
    except Exception:  # noqa: BLE001
        key = None
    if key and key in s:
        s = s.replace(key, "[key]")
    return s[:limit]


def clean_text(value, limit):
    """Vendor text made safe to show: tags and markdown links stripped, one line, clipped."""
    if not isinstance(value, str):
        return ""
    t = _TAG_RE.sub(" ", value)
    t = _MD_IMG_RE.sub("", t)          # pictures first, so [![alt](img)](link) leaves nothing behind
    t = _MD_LINK_RE.sub(r"\1", t)
    t = _CTRL_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    if len(t) > limit:
        t = t[:max(1, limit - 1)].rstrip() + _ELLIPSIS
    return t


def _plain_error(reason):
    return {
        "bad_key": "CurseForge refused the API key (it is wrong, expired or revoked). "
                   "Check it in Settings > Integrations > Minecraft mods.",
        "not_found": "CurseForge has nothing under that id.",
        "rate_limited": "CurseForge says there were too many requests. Wait a little and try again.",
        "offline": "CurseForge did not answer (the network is down or its server had an error).",
        "vendor_changed": "CurseForge answered in a shape this version of Jarvis does not understand.",
    }.get(reason, "CurseForge could not be asked just now.")


def _status_reason(status):
    if status in (401, 403):
        return "bad_key"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if isinstance(status, int) and 400 <= status < 500:
        return "vendor_changed"
    return "offline"


def _need_key():
    """None when a usable key exists, else the failure dict with the how-to-get-one sentence.
    No request is made without a key (the vendor would answer 403 anyway)."""
    try:
        if config.get_key():
            return None
        problem = config.key_problem()
    except Exception:  # noqa: BLE001
        problem = ""
    text = config.no_key_message()
    return _failure("no_key", (problem + " " + text) if problem else text)


def _fetcher(fetch):
    """(callable, None) or (None, failure). The real client is imported only when needed."""
    if fetch is not None:
        return fetch, None
    try:
        from . import net
        return net.fetch, None
    except Exception:  # noqa: BLE001 -- net.py missing or broken: say so, never raise
        return None, _failure("unsupported", "The guarded network client for mod sites is not available in this build.")


def _call(fetch, path, params=None, json_body=None, method="GET"):
    """One request. Returns (body, failure, status): exactly one of body / failure is set
    (a body can be any JSON value). `status` is the HTTP status when known. Never raises."""
    fn, bad = _fetcher(fetch)
    if bad:
        return None, bad, None
    try:
        res = fn(VENDOR, API_BASE + path, method=method, params=params, json_body=json_body)
    except Exception as exc:  # noqa: BLE001
        return None, _failure("offline", "CurseForge could not be reached (%s)." % type(exc).__name__), None
    if not isinstance(res, dict):
        return None, _failure("vendor_changed", _plain_error("vendor_changed")), None
    status = res.get("status") if isinstance(res.get("status"), int) and not isinstance(res.get("status"), bool) else None
    if res.get("ok") is True and (status is None or status < 400):
        return res.get("body"), None, status
    reason = res.get("reason") if res.get("reason") in _REASONS else None
    if reason is None:
        reason = _status_reason(status) if status is not None else "offline"
    error = _scrub(res["error"]) if isinstance(res.get("error"), str) and res.get("error") else _plain_error(reason)
    extra = {}
    if isinstance(res.get("retry_after"), (int, float)) and not isinstance(res.get("retry_after"), bool):
        extra["retry_after"] = res["retry_after"]
    return None, _failure(reason, error, **extra), status


def _data(body):
    """body['data'] when the answer has the documented envelope, else None."""
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return None


def _int_id(value):
    """A positive integer id from an int or a digit string; None for anything else (so a
    hand-typed ref can never become part of a URL path)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and len(text) <= 10:
            n = int(text)
            return n if n > 0 else None
    return None


def _vkey(version):
    return tuple(int(p) for p in version.split("."))


def classify_game_versions(entries):
    """Split a file's ``gameVersions`` list (one list that mixes three kinds of entry).

    Returns ``{"minecraft": [exact version strings, newest first], "loaders": [lower-case
    loader names, in the order fabric, forge, neoforge, quilt], "other": [everything else:
    "Client", "Server", "Java 21", snapshots, ...]}``. Never assumes a position; never
    guesses: an entry is a Minecraft version only if it is digits and dots (two or three
    parts), a loader only if it is exactly one of the four loader names.
    """
    mc, loaders, other = [], set(), []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        low = text.lower()
        if _MC_RE.match(text):
            if text not in mc:
                mc.append(text)
        elif low in LOADER_TYPES:
            loaders.add(low)
        elif text and text not in other:
            other.append(text)
    mc.sort(key=_vkey, reverse=True)
    return {"minecraft": mc, "loaders": [n for n in LOADER_ORDER if n in loaders], "other": other}


def _cdn_hosts():
    """The CDN host names a download link may use: the host list in hosts.py minus the API
    host, or the one host the probe saw when hosts.py is not there yet."""
    try:
        from . import hosts
        names = getattr(hosts, "CURSEFORGE_HOSTS", None)
        if names:
            found = {str(n).lower() for n in names} - {API_HOST}
            if found:
                return found
    except Exception:  # noqa: BLE001
        pass
    return {"edge.forgecdn.net"}


def _link_ok(url):
    """An https link on an allowed CDN host with no credentials, port or odd characters."""
    m = re.match(r"^https://([A-Za-z0-9.\-]+)(/[^\s\x00-\x1f]*)?$", url or "")
    return bool(m) and m.group(1).lower() in _cdn_hosts()


def _page_link(url):
    return url if isinstance(url, str) and re.match(r"^https://(www\.)?curseforge\.com/[^\s\x00-\x1f]*$", url) else None


# ============================================================================================
# vendor dicts -> contract records
# ============================================================================================
def _project(mod):
    """A contract project record, or None when `mod` is not one (or is of a class these tools
    do not handle, such as worlds)."""
    if not isinstance(mod, dict) or _int_id(mod.get("id")) is None:
        return None
    kind = KIND_OF_CLASS.get(mod.get("classId"))
    if kind is None:
        return None
    slug = mod.get("slug") if isinstance(mod.get("slug"), str) else ""
    mc, loaders = set(), set()
    for idx in mod.get("latestFilesIndexes") if isinstance(mod.get("latestFilesIndexes"), list) else []:
        if not isinstance(idx, dict):
            continue
        if isinstance(idx.get("gameVersion"), str) and _MC_RE.match(idx["gameVersion"].strip()):
            mc.add(idx["gameVersion"].strip())
        name = LOADER_OF_TYPE.get(idx.get("modLoader"))
        if name:
            loaders.add(name)
    for f in mod.get("latestFiles") if isinstance(mod.get("latestFiles"), list) else []:
        if isinstance(f, dict):
            got = classify_game_versions(f.get("gameVersions"))
            mc.update(got["minecraft"])
            loaders.update(got["loaders"])
    authors = mod.get("authors") if isinstance(mod.get("authors"), list) else []
    author = ""
    for a in authors:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].strip():
            author = clean_text(a["name"], 60)
            break
    links = mod.get("links") if isinstance(mod.get("links"), dict) else {}
    page = _page_link(links.get("websiteUrl"))
    if not page:
        page = "https://www.curseforge.com/minecraft/%s/%s" % (CLASS_PAGE_SLUG[kind], slug) \
            if _SLUG_RE.match(slug) else "https://www.curseforge.com/minecraft"
    allow = mod.get("allowModDistribution")
    categories = []
    for c in mod.get("categories") if isinstance(mod.get("categories"), list) else []:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            name = clean_text(c["name"], 40)
            if name and name not in categories:
                categories.append(name)
    downloads = mod.get("downloadCount")
    return {
        "source": SOURCE,
        "id": str(_int_id(mod.get("id"))),
        "slug": slug,
        "name": clean_text(mod.get("name"), 100),
        "summary": clean_text(mod.get("summary"), 160),
        "author": author,
        "downloads": int(downloads) if isinstance(downloads, (int, float)) and not isinstance(downloads, bool) else 0,
        "updated": mod.get("dateModified") if isinstance(mod.get("dateModified"), str) else "",
        "kind": kind,
        "mc_versions": sorted(mc, key=_vkey, reverse=True)[:6],
        "loaders": [n for n in LOADER_ORDER if n in loaders],
        "page_url": page,
        "distributable": allow if isinstance(allow, bool) else None,
        # CurseForge does not say which side a project runs on.
        "client_side": "unknown",
        "server_side": "unknown",
        # extras for get_mc_project (not part of the minimum record)
        "categories": categories[:8],
        "links": {k: v for k, v in (
            ("website", page),
            ("source", _https_only(links.get("sourceUrl"))),
            ("issues", _https_only(links.get("issuesUrl"))),
            ("wiki", _https_only(links.get("wikiUrl")))) if v},
    }


def _https_only(url):
    return url if isinstance(url, str) and re.match(r"^https://[^\s\x00-\x1f]{1,300}$", url) else None


def _dependencies(raw):
    """CurseForge relation types -> the contract's four: 3 required; 2 optional and 4 tool
    -> optional; 1 embedded library and 6 include -> embedded; 5 incompatible."""
    kinds = {REL_REQUIRED: "required", REL_OPTIONAL: "optional", REL_TOOL: "optional",
             REL_EMBEDDED: "embedded", REL_INCLUDE: "embedded", REL_INCOMPATIBLE: "incompatible"}
    out, seen = [], set()
    for dep in raw if isinstance(raw, list) else []:
        if not isinstance(dep, dict):
            continue
        pid, kind = _int_id(dep.get("modId")), kinds.get(dep.get("relationType"))
        if pid is None or kind is None or (pid, kind) in seen:
            continue
        seen.add((pid, kind))
        out.append({"project_id": str(pid), "file_id": None, "type": kind})
    return out


def _file(raw, project_id=None):
    """A contract file record, or None when `raw` is not a file."""
    if not isinstance(raw, dict) or _int_id(raw.get("id")) is None:
        return None
    pid = _int_id(raw.get("modId")) or _int_id(project_id)
    if pid is None:
        return None
    parsed = classify_game_versions(raw.get("gameVersions"))
    url = raw.get("downloadUrl")
    why = ""
    if not isinstance(url, str) or not url.strip():
        # A null link is the author's choice (allowModDistribution false): not downloadable
        # here, and never worked around.
        url, why = None, "no_url"
    elif not _link_ok(url.strip()):
        url, why = None, "host_not_allowed"
    elif raw.get("isAvailable") is False or raw.get("fileStatus") in BAD_FILE_STATUS:
        url, why = None, "unavailable"
    else:
        url = url.strip()
    hashes = {}
    for h in raw.get("hashes") if isinstance(raw.get("hashes"), list) else []:
        # Only SHA-1 (algo 1) can be verified; MD5 (algo 2) is the only other kind CurseForge
        # publishes and is deliberately left out so nobody picks it by accident.
        if isinstance(h, dict) and h.get("algo") == HASH_SHA1 and isinstance(h.get("value"), str):
            value = h["value"].strip().lower()
            if _SHA1_RE.match(value):
                hashes["sha1"] = value
    fp = raw.get("fileFingerprint")
    if isinstance(fp, int) and not isinstance(fp, bool):
        hashes["fingerprint"] = str(fp)        # CurseForge's own murmur2 hash, not a checksum
    size = raw.get("fileLength")
    out = {
        "source": SOURCE,
        "project_id": str(pid),
        "file_id": str(_int_id(raw.get("id"))),
        "file_name": _CTRL_RE.sub("", raw["fileName"]) if isinstance(raw.get("fileName"), str) else "",
        "version_label": clean_text(raw.get("displayName"), 120),
        "mc_versions": parsed["minecraft"],
        "loaders": parsed["loaders"],
        "channel": CHANNELS.get(raw.get("releaseType"), "alpha"),   # unknown = never preferred
        "date": raw.get("fileDate") if isinstance(raw.get("fileDate"), str) else "",
        "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
        "download_url": url,
        "downloadable": url is not None,
        "hashes": hashes,
        "dependencies": _dependencies(raw.get("dependencies")),
    }
    if why:
        out["why"] = why        # no_url | host_not_allowed | unavailable (only when not downloadable)
    return out


def _is_server_pack(raw):
    return isinstance(raw, dict) and raw.get("isServerPack") is True


def _newest_first(files):
    files.sort(key=lambda f: (f.get("date") or "", int(f["file_id"])), reverse=True)
    return files


def _check_filters(mc_version, loader):
    """(mc, loader_name, failure). Both optional; a malformed one is refused, not guessed."""
    mc = None
    if mc_version not in (None, ""):
        mc = str(mc_version).strip()
        if not _MC_RE.match(mc):
            return None, None, _failure("unsupported", "That does not look like a Minecraft version (for example 1.21.11 or 26.3).")
    name = None
    if loader not in (None, ""):
        name = str(loader).strip().lower()
        if name not in LOADER_TYPES:
            return None, None, _failure("unsupported", "CurseForge lists fabric, forge, neoforge and quilt; \"%s\" is none of them." % clean_text(str(loader), 20))
    return mc, name, None


# ============================================================================================
# the source adapter
# ============================================================================================
def search(query, kind="mod", mc_version=None, loader=None, sort="relevance", limit=5, offset=0, *, fetch=None):
    """-> {"ok": True, "hits": [project], "total": int}. Search filters are honoured by the
    vendor (probe), but the loader filter is not exact: see list_files for the real check."""
    try:
        bad = _need_key()
        if bad:
            return bad
        class_id = CLASS_IDS.get(kind)
        if class_id is None:
            return _failure("unsupported", "CurseForge search covers mods, modpacks, resource packs, shaders and data packs.")
        mc, loader_name, bad = _check_filters(mc_version, loader)
        if bad:
            return bad
        page = max(1, min(int(limit), SEARCH_PAGE_MAX)) if isinstance(limit, int) and not isinstance(limit, bool) else 5
        start = int(offset) if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0 else 0
        params = {"gameId": GAME_ID, "classId": class_id, "sortField": SORT_FIELDS.get(sort, SORT_FIELDS["relevance"]),
                  "sortOrder": "desc", "pageSize": page, "index": start}
        text = clean_text(query, MAX_QUERY_CHARS) if isinstance(query, str) else ""
        if text:
            params["searchFilter"] = text
        if mc:
            params["gameVersion"] = mc
        if loader_name and kind in ("mod", "modpack"):       # shaders, resource and data packs have no loader
            params["modLoaderType"] = LOADER_TYPES[loader_name]
        body, bad, _status = _call(fetch, "/mods/search", params=params)
        if bad:
            return bad
        data = _data(body)
        if not isinstance(data, list):
            return _failure("vendor_changed", _plain_error("vendor_changed"))
        hits = [p for p in (_project(m) for m in data) if p]
        total = None
        if isinstance(body.get("pagination"), dict):
            total = body["pagination"].get("totalCount")
        return {"ok": True, "hits": hits,
                "total": total if isinstance(total, int) and not isinstance(total, bool) else len(hits)}
    except Exception as exc:  # noqa: BLE001 -- the contract: never raise
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


def get_project(ref, *, fetch=None):
    """-> {"ok": True, "project": project}. `ref` is a numeric id or a slug. A slug that
    matches two projects (a mod and a modpack can share one) is `ambiguous` with the
    candidates, never a guess."""
    try:
        bad = _need_key()
        if bad:
            return bad
        pid = _int_id(ref)
        if pid is not None:
            body, bad, status = _call(fetch, "/mods/%d" % pid)
            if bad:
                return bad
            data = _data(body)
            if not isinstance(data, dict):
                return _failure("vendor_changed", _plain_error("vendor_changed"))
            project = _project(data)
            if project is None:
                return _failure("unsupported", "That CurseForge project is not a mod, modpack, resource pack, shader or data pack (a world, for example), which these tools do not handle.")
            return {"ok": True, "project": project}
        slug = str(ref).strip().lower() if isinstance(ref, str) else ""
        if not _SLUG_RE.match(slug):
            return _failure("not_found", "That is not a CurseForge project id or slug.")
        return _by_slug(slug, fetch)
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


def _by_slug(slug, fetch):
    """The slug route: the search's own `slug` parameter first (in the swagger, never run by
    the probe), then a plain text search; only an EXACT slug match counts either way."""
    found, unsupported = {}, False
    for extra in ({"slug": slug}, {"searchFilter": slug}):
        params = {"gameId": GAME_ID, "sortField": SORT_FIELDS["relevance"], "sortOrder": "desc", "pageSize": SEARCH_PAGE_MAX}
        params.update(extra)
        body, bad, _status = _call(fetch, "/mods/search", params=params)
        if bad:
            return bad
        data = _data(body)
        if not isinstance(data, list):
            return _failure("vendor_changed", _plain_error("vendor_changed"))
        for mod in data:
            if isinstance(mod, dict) and isinstance(mod.get("slug"), str) and mod["slug"].lower() == slug:
                project = _project(mod)
                if project:
                    found[project["id"]] = project
                else:
                    unsupported = True
        if found:
            break
    if len(found) == 1:
        return {"ok": True, "project": next(iter(found.values()))}
    if found:
        return _failure("ambiguous", "More than one CurseForge project has the slug \"%s\"; pick one by its id." % slug,
                        candidates=list(found.values()))
    if unsupported:
        return _failure("unsupported", "That CurseForge project is not a mod, modpack, resource pack, shader or data pack, which these tools do not handle.")
    return _failure("not_found", "CurseForge has no project with the slug \"%s\"." % slug)


def list_files(project_id, mc_version=None, loader=None, *, fetch=None):
    """-> {"ok": True, "files": [file]}: newest first, every channel, server packs left out.

    `mc_version` and `loader` are exact: the request carries them as hints, then each file's
    own ``gameVersions`` is parsed and checked (the vendor's loader filter lets Fabric files
    through for Quilt and the reverse). A file counts as Quilt only if it is tagged "Quilt"
    itself; offering a Fabric file for Quilt is resolve.pick_file's job (labelled ``via``),
    from a list fetched with the loader filter off.
    """
    try:
        bad = _need_key()
        if bad:
            return bad
        pid = _int_id(project_id)
        if pid is None:
            return _failure("not_found", "That is not a CurseForge project id.")
        mc, loader_name, bad = _check_filters(mc_version, loader)
        if bad:
            return bad
        files, index = [], 0
        for _page in range(FILES_MAX_PAGES):
            params = {"pageSize": FILES_PAGE_SIZE, "index": index}
            if mc:
                params["gameVersion"] = mc
            if loader_name:
                params["modLoaderType"] = LOADER_TYPES[loader_name]
            body, bad, _status = _call(fetch, "/mods/%d/files" % pid, params=params)
            if bad:
                return bad
            data = _data(body)
            if not isinstance(data, list):
                return _failure("vendor_changed", _plain_error("vendor_changed"))
            for raw in data:
                if _is_server_pack(raw):
                    continue
                rec = _file(raw, pid)
                if rec:
                    files.append(rec)
            index += len(data)
            total = body["pagination"].get("totalCount") if isinstance(body.get("pagination"), dict) else None
            if len(data) < FILES_PAGE_SIZE or (isinstance(total, int) and index >= total):
                break
        if mc:
            files = [f for f in files if mc in f["mc_versions"]]
        if loader_name:
            files = [f for f in files if loader_name in f["loaders"]]
        return {"ok": True, "files": _newest_first(files)}
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


def get_file(project_id, file_id, *, fetch=None):
    """-> {"ok": True, "file": file}. The file must belong to `project_id` (a mismatch is
    `not_found`); a server pack is `unsupported`."""
    try:
        bad = _need_key()
        if bad:
            return bad
        pid, fid = _int_id(project_id), _int_id(file_id)
        if pid is None or fid is None:
            return _failure("not_found", "That is not a CurseForge project id and file id.")
        body, bad, _status = _call(fetch, "/mods/%d/files/%d" % (pid, fid))
        if bad:
            return bad
        data = _data(body)
        if not isinstance(data, dict):
            return _failure("vendor_changed", _plain_error("vendor_changed"))
        if _is_server_pack(data):
            return _failure("unsupported", "That CurseForge file is a server pack, not the file for a game client.")
        rec = _file(data, pid)
        if rec is None or rec["project_id"] != str(pid):
            return _failure("not_found", "CurseForge has no such file in that project.")
        return {"ok": True, "file": rec}
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def bulk_projects(refs, *, fetch=None):
    """-> {"ok": True, "projects": {ref: project | None}}. Numeric ids go out in bulk
    (``POST /mods``, 50 per call); a slug has no bulk route and costs one lookup each. A ref
    that is unknown, ambiguous or of a class these tools do not handle maps to None (use
    get_project / resolve_project to learn which)."""
    try:
        bad = _need_key()
        if bad:
            return bad
        refs = list(refs) if isinstance(refs, (list, tuple, set)) else []
        out, ids = {}, []
        for ref in refs:
            n = _int_id(ref)
            if n is not None:
                if n not in ids:
                    ids.append(n)
            out[ref] = None
        by_id = {}
        for chunk in _chunks(ids, BULK_CHUNK):
            body, bad, _status = _call(fetch, "/mods", method="POST", json_body={"modIds": chunk, "filterPcOnly": True})
            if bad:
                return bad
            data = _data(body)
            if not isinstance(data, list):
                return _failure("vendor_changed", _plain_error("vendor_changed"))
            for mod in data:
                project = _project(mod)
                if project:
                    by_id[project["id"]] = project
        for ref in refs:
            n = _int_id(ref)
            if n is not None:
                out[ref] = by_id.get(str(n))
            elif isinstance(ref, str):
                got = get_project(ref, fetch=fetch)
                if got.get("ok"):
                    out[ref] = got["project"]
                elif got.get("reason") in _FATAL:
                    return got
        return {"ok": True, "projects": out}
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


def bulk_list_files(project_ids, mc_version=None, loader=None, *, fetch=None):
    """-> {"ok": True, "files": {project_id: [file]}}.

    One filtered ``GET /mods/{id}/files`` per project: CurseForge has no "files of these
    projects" route, and the bulk ``POST /mods`` answer's ``latestFilesIndexes`` was not probed
    well enough to say it holds the newest file of EVERY channel (a project whose newest file
    is an alpha could look release-less). The answers are small (about 2 KB filtered). A
    project that is unknown or of an unsupported class gets an empty list; a failure that
    would hit every project (no key, bad key, rate limit, offline) fails the whole call.
    """
    try:
        bad = _need_key()
        if bad:
            return bad
        out = {}
        for pid in project_ids if isinstance(project_ids, (list, tuple, set)) else []:
            got = list_files(pid, mc_version, loader, fetch=fetch)
            if got.get("ok"):
                out[pid] = got["files"]
            elif got.get("reason") in _FATAL:
                return got
            else:
                out[pid] = []
        return {"ok": True, "files": out}
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


def bulk_get_files(pairs, *, fetch=None):
    """-> {"ok": True, "files": {"<project_id>:<file_id>": file | None}} via ``POST /mods/files``
    (50 ids per call). A file the vendor does not return, one that belongs to another project
    than the pair says, and a server pack are None."""
    try:
        bad = _need_key()
        if bad:
            return bad
        wanted, order = {}, []
        for pair in pairs if isinstance(pairs, (list, tuple, set)) else []:
            try:
                pid, fid = _int_id(pair[0]), _int_id(pair[1])
            except (TypeError, IndexError, KeyError):
                continue
            if pid is None or fid is None:
                continue
            key = "%d:%d" % (pid, fid)
            if key not in wanted:
                wanted[key] = (pid, fid)
                order.append(key)
        out = {k: None for k in order}
        fids = []
        for k in order:
            if wanted[k][1] not in fids:
                fids.append(wanted[k][1])
        got = {}
        for chunk in _chunks(fids, BULK_CHUNK):
            body, bad, _status = _call(fetch, "/mods/files", method="POST", json_body={"fileIds": chunk})
            if bad:
                return bad
            data = _data(body)
            if not isinstance(data, list):
                return _failure("vendor_changed", _plain_error("vendor_changed"))
            for raw in data:
                if _is_server_pack(raw):
                    continue
                rec = _file(raw)
                if rec:
                    got[(int(rec["project_id"]), int(rec["file_id"]))] = rec
        for k in order:
            out[k] = got.get(wanted[k])
        return {"ok": True, "files": out}
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


# ============================================================================================
# extras beyond the contract (the contract's functions never need them)
# ============================================================================================
def download_url(project_id, file_id, *, fetch=None):
    """-> {"ok": True, "download_url": url} from the official ``.../download-url`` route, the
    one the probe ran. A 403 (empty body) or an empty answer means the author does not allow
    third-party downloads: reason `not_distributable`, and nothing else is tried (no Curse Maven,
    no website routes). The records from list_files / get_file never call this route; a null
    ``downloadUrl`` there is simply "not downloadable here"."""
    try:
        bad = _need_key()
        if bad:
            return bad
        pid, fid = _int_id(project_id), _int_id(file_id)
        if pid is None or fid is None:
            return _failure("not_found", "That is not a CurseForge project id and file id.")
        body, bad, status = _call(fetch, "/mods/%d/files/%d/download-url" % (pid, fid))
        if bad:
            # The route says 403 for a file the author keeps off third-party tools. (If the key
            # itself were refused, every other call would already have said so.)
            if status == 403 or bad.get("reason") == "bad_key":
                return _failure("not_distributable", "The author of this CurseForge project does not allow downloads outside CurseForge. Use the project page instead.")
            return bad
        url = _data(body)
        if not isinstance(url, str) or not url.strip():
            return _failure("not_distributable", "CurseForge gives no download link for this file (the author does not allow downloads outside CurseForge). Use the project page instead.")
        if not _link_ok(url.strip()):
            return _failure("vendor_changed", "CurseForge gave a download link on a host Jarvis does not allow.")
        return {"ok": True, "download_url": url.strip()}
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)


def ping(*, fetch=None):
    """-> {"ok": True} when CurseForge answers and accepts the key (one small request, the
    class list the probe ran). For ``jarvis doctor --deep``; never used by a tool."""
    try:
        bad = _need_key()
        if bad:
            return bad
        body, bad, _status = _call(fetch, "/categories", params={"gameId": GAME_ID, "classesOnly": "true"})
        if bad:
            return bad
        if not isinstance(_data(body), list):
            return _failure("vendor_changed", _plain_error("vendor_changed"))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return _failure("vendor_changed", "CurseForge's answer could not be read (%s)." % type(exc).__name__)
