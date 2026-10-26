"""The Modrinth adapter (no key needed).  Same module-level functions as every source adapter (see sources.py).

All traffic goes through net.fetch (vendor "modrinth"); answers are cached for about ten minutes in cache.py.
Everything a vendor wrote (names, summaries, file names) is cleaned by untrusted.py on the way in, and the
project / file dicts carry only the contract's fields (plus `categories` and `links` on a project, which the
get_mc_project tool shows).

What the owner's probe (25 Oct 2026) verified, and what is still a guess
  verified  GET /search with query, facets (JSON array of arrays), limit, index; response hits / limit / offset /
            total_hits; facets are honoured.  GET /project/{slug}/version with `loaders` and `game_versions`, each
            a JSON array in the URL, honoured (always filter: the unfiltered list for sodium is ~526 KB).
            GET /versions?ids=[..], GET /projects?ids=[..], GET /version/{id}.  Files carry hashes {sha1, sha512};
            downloads come from cdn.modrinth.com.  Data packs come back as project_type=mod with category
            `datapack`.  A Quilt query never returns Fabric files.
  PROBE     the names in PARAM below that the probe did not exercise: the `offset` request parameter of /search
            (the response key is verified), and the `ids` parameter of /projects and /versions when it holds
            slugs (ids were tested).  GET /project/{id}/members (used only for the author's name; a failure
            there leaves the author empty).  Every parameter NAME is isolated in PARAM so a probe result is a
            one-line change.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

from . import (NOT_FOUND, OFFLINE, RATE_LIMITED, UNSUPPORTED, VENDOR_CHANGED, cache, fail, net, untrusted)
from .resolve import normalise_loader, normalise_mc_version, version_key
from .sources import KINDS, LOADERS

VENDOR = "modrinth"
BASE = "https://api.modrinth.com/v2"
SITE = "https://modrinth.com"
CACHE_TTL = 600
BATCH = 100                 # ids per bulk request (the URL stays short; Modrinth does not publish a smaller limit)
SEARCH_MAX = 100            # Modrinth's own page size ceiling
WORKERS = 4                 # parallel per-project file lists in bulk_list_files (Modrinth allows 300 requests/min)
FEW_VERSIONS = 5

# ---- every query parameter NAME and facet key in one place (see PROBE above) --------------------------------
PARAM = {
    "search_query": "query", "search_facets": "facets", "search_limit": "limit", "search_offset": "offset",
    "search_index": "index", "versions_loaders": "loaders", "versions_game_versions": "game_versions",
    "bulk_ids": "ids",
}
FACET = {"type": "project_type", "category": "categories", "version": "versions"}
SORTS = {"relevance": "relevance", "downloads": "downloads", "updated": "updated", "newest": "newest"}
# project_type of each kind; a data pack is a `mod` with the `datapack` category (verified)
TYPE_OF_KIND = {"mod": "mod", "modpack": "modpack", "resourcepack": "resourcepack", "shader": "shader",
                "datapack": "mod"}
# ---------------------------------------------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9]{8}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,200}$")
_SIDES = ("required", "optional", "unsupported")
_DEP_TYPES = ("required", "optional", "incompatible", "embedded")
_PAGE_TYPES = ("mod", "modpack", "resourcepack", "shader", "datapack", "plugin")
_BAD_STATUS = ("draft", "rejected", "scheduled", "withheld")


# --------------------------------------------------------------------------- one cached GET

def _get(path, params=None, ttl=CACHE_TTL):
    """-> {"ok": True, "body": <json>} | failure dict.  Cached by path + parameters."""
    key = "modrinth:%s?%s" % (path, json.dumps(sorted((params or {}).items()), separators=(",", ":")))
    hit = cache.get(key)
    if hit is not None:
        return {"ok": True, "body": hit, "cached": True}
    reply = net.fetch(VENDOR, BASE + path, params=params)
    if not reply.get("ok"):
        return reply
    body = reply.get("body")
    if body is not None and ttl:
        cache.put(key, body, ttl)
    return {"ok": True, "body": body}


def _bad_ref(ref):
    return fail(NOT_FOUND, "%r is not a Modrinth project id or slug." % str(ref)[:40])


# --------------------------------------------------------------------------- shaping (vendor -> contract)

def _slug(value):
    value = str(value or "")
    return value if _SLUG_RE.match(value) else ""


def _kind(project_type, categories):
    cats = [str(c).lower() for c in categories or []]
    ptype = str(project_type or "").lower()
    if ptype == "datapack" or (ptype == "mod" and "datapack" in cats):
        return "datapack"
    return ptype if ptype in ("mod", "modpack", "resourcepack", "shader") else "mod"


def _side(value):
    value = str(value or "").lower()
    return value if value in _SIDES else "unknown"


def _few(versions, prefer=None):
    """A few Minecraft versions, newest first, exact strings.  `prefer` (the version asked for) is kept in."""
    unique = sorted({str(v) for v in versions or [] if isinstance(v, str)}, key=version_key, reverse=True)
    few = unique[:FEW_VERSIONS]
    if prefer and prefer in unique and prefer not in few:
        few = [prefer] + few[:FEW_VERSIONS - 1]
    return few


def _page_url(project_type, slug):
    ptype = str(project_type or "mod").lower()
    ptype = ptype if ptype in _PAGE_TYPES else "mod"
    return "%s/%s/%s" % (SITE, ptype, quote(slug, safe="")) if slug else ""


def _int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _project(obj, prefer_mc=None, hit=False):
    """A search hit (hit=True) or a /project answer -> the contract's project dict."""
    if not isinstance(obj, dict):
        return None
    pid = obj.get("project_id") if hit else obj.get("id")
    pid = str(pid or obj.get("id") or obj.get("project_id") or "")
    if not pid:
        return None
    categories = [str(c).lower() for c in (obj.get("categories") or []) + (obj.get("additional_categories") or [])
                  if isinstance(c, str)]
    slug = _slug(obj.get("slug"))
    ptype = str(obj.get("project_type") or "mod")
    loaders = [c for c in (obj.get("loaders") or categories) if isinstance(c, str) and c.lower() in LOADERS]
    versions = obj.get("versions") if hit else obj.get("game_versions")
    out = {
        "source": "modrinth",
        "id": pid,
        "slug": slug,
        "name": untrusted.clean_name(obj.get("title") or slug or pid),
        "summary": untrusted.clean(obj.get("description"), untrusted.SUMMARY_LEN),
        "author": untrusted.clean_name(obj.get("author") or "", 40),
        "downloads": _int(obj.get("downloads")),
        "updated": str((obj.get("date_modified") if hit else obj.get("updated")) or "")[:40],
        "kind": _kind(ptype, categories),
        "mc_versions": _few(versions, prefer_mc),
        "loaders": sorted({c.lower() for c in loaders}),
        "page_url": _page_url(ptype, slug),
        "distributable": True,
        "client_side": _side(obj.get("client_side")),
        "server_side": _side(obj.get("server_side")),
        "categories": [untrusted.clean_name(c, 30) for c in categories][:12],
    }
    if not hit:
        links = {}
        for label, field in (("issues", "issues_url"), ("source", "source_url"), ("wiki", "wiki_url"),
                             ("discord", "discord_url")):
            url = obj.get(field)
            if isinstance(url, str) and url.lower().startswith("https://") and len(url) <= 300:
                links[label] = url
        out["links"] = links
    return out


def _hash_dict(raw):
    out = {}
    for name, value in (raw or {}).items():
        if isinstance(name, str) and isinstance(value, str) and _HEX_RE.match(value):
            out[name.lower()] = value.lower()
    return out


def _download_url(url):
    """The file URL when it is https on a Modrinth download host, else None (never a link elsewhere)."""
    try:
        parts = urlsplit(str(url or ""))
        if parts.scheme == "https" and (parts.hostname or "").lower() in net.allowed_hosts(VENDOR):
            return str(url)
    except ValueError:
        pass
    return None


def _file(version):
    """A Modrinth version -> the contract's file dict (its primary file), or None when it has no file."""
    if not isinstance(version, dict) or str(version.get("status") or "listed").lower() in _BAD_STATUS:
        return None
    files = [f for f in version.get("files") or [] if isinstance(f, dict)]
    if not files:
        return None
    primary = next((f for f in files if f.get("primary")), files[0])
    url = _download_url(primary.get("url"))
    channel = str(version.get("version_type") or "release").lower()
    deps = []
    for dep in version.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        kind = str(dep.get("dependency_type") or "").lower()
        if kind not in _DEP_TYPES:
            continue
        pid, fid = dep.get("project_id"), dep.get("version_id")
        if not pid and not fid:
            continue
        deps.append({"project_id": str(pid or ""), "file_id": str(fid) if fid else None, "type": kind})
    size = primary.get("size")
    return {
        "source": "modrinth",
        "project_id": str(version.get("project_id") or ""),
        "file_id": str(version.get("id") or ""),
        "file_name": untrusted.clean_file_name(primary.get("filename")),
        "version_label": untrusted.clean_name(version.get("version_number") or version.get("name") or "", 60),
        "mc_versions": [str(v) for v in version.get("game_versions") or [] if isinstance(v, str)],
        "loaders": [str(x).lower() for x in version.get("loaders") or [] if isinstance(x, str)],
        "channel": channel if channel in ("release", "beta", "alpha") else "alpha",
        "date": str(version.get("date_published") or "")[:40],
        "size": size if isinstance(size, int) and size >= 0 else None,
        "download_url": url,
        "downloadable": bool(url),
        "hashes": _hash_dict(primary.get("hashes")),
        "dependencies": deps,
    }


def _files_from(versions):
    out = [f for f in (_file(v) for v in versions or []) if f and f["file_id"]]
    out.sort(key=lambda f: f["date"], reverse=True)          # ISO dates sort as text
    return out


# --------------------------------------------------------------------------- the adapter

def search(query, kind="mod", mc_version=None, loader=None, sort="relevance", limit=5, offset=0):
    """-> {"ok": True, "hits": [project], "total": int} | failure"""
    try:
        kind = str(kind or "mod").lower()
        if kind not in KINDS:
            return fail(UNSUPPORTED, "Unknown kind %r: use mod, modpack, resourcepack, shader or datapack." % kind)
        facets = [["%s:%s" % (FACET["type"], TYPE_OF_KIND[kind])]]
        if kind == "datapack":
            facets.append(["%s:datapack" % FACET["category"]])
        if loader not in (None, ""):
            norm = normalise_loader(loader)
            if norm is None:
                return fail(UNSUPPORTED, "%r is not a loader Jarvis knows: use fabric, forge, neoforge or quilt."
                            % str(loader)[:30])
            if kind in ("mod", "modpack"):             # shaders / packs have no loader of this kind
                facets.append(["%s:%s" % (FACET["category"], norm)])
        mc = None
        if mc_version not in (None, ""):
            mc = normalise_mc_version(mc_version)
            if mc is None:
                return fail(UNSUPPORTED, "%r is not a Minecraft version like 1.21.1 or 26.3." % str(mc_version)[:30])
            facets.append(["%s:%s" % (FACET["version"], mc)])
        try:
            page = max(1, min(int(limit), SEARCH_MAX))
            skip = max(0, int(offset))
        except (TypeError, ValueError):
            page, skip = 5, 0
        params = {
            PARAM["search_query"]: str(query or "")[:200],
            PARAM["search_facets"]: json.dumps(facets, separators=(",", ":")),
            PARAM["search_limit"]: page,
            PARAM["search_index"]: SORTS.get(str(sort or "relevance").lower(), "relevance"),
        }
        if skip:
            params[PARAM["search_offset"]] = skip
        got = _get("/search", params)
        if not got.get("ok"):
            return got
        body = got.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("hits"), list):
            return fail(VENDOR_CHANGED, "Modrinth's search answer is not in the shape Jarvis expects.")
        hits = [p for p in (_project(h, mc, hit=True) for h in body["hits"]) if p]
        if kind == "mod":                              # a data pack is a `mod` here: not what "mod" asked for
            hits = [p for p in hits if p["kind"] != "datapack"]
        return {"ok": True, "hits": hits, "total": _int(body.get("total_hits"))}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth search could not be read.")


def _author_of(project_id):
    """The project owner's user name (one cached call); '' when it cannot be had."""
    got = _get("/project/%s/members" % quote(project_id, safe=""))
    members = got.get("body") if got.get("ok") else None
    if not isinstance(members, list):
        return ""
    for member in members:
        if isinstance(member, dict) and str(member.get("role") or "").lower() == "owner":
            user = member.get("user") if isinstance(member.get("user"), dict) else {}
            return untrusted.clean_name(user.get("username") or "", 40)
    for member in members:
        user = member.get("user") if isinstance(member, dict) and isinstance(member.get("user"), dict) else {}
        if user.get("username"):
            return untrusted.clean_name(user["username"], 40)
    return ""


def get_project(ref):
    """ref = id or slug -> {"ok": True, "project": project} | failure"""
    try:
        text = str(ref or "").strip()
        if not _SLUG_RE.match(text):
            return _bad_ref(ref)
        got = _get("/project/%s" % quote(text, safe=""))
        if not got.get("ok"):
            return got
        project = _project(got.get("body"))
        if project is None:
            return fail(VENDOR_CHANGED, "Modrinth's project answer is not in the shape Jarvis expects.")
        project["author"] = _author_of(project["id"])
        return {"ok": True, "project": project}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth project could not be read.")


def list_files(project_id, mc_version=None, loader=None):
    """Newest first, ALL channels.  The exact mc_version / loader filter is applied by Modrinth when given."""
    try:
        text = str(project_id or "").strip()
        if not _SLUG_RE.match(text):
            return _bad_ref(project_id)
        params = {}
        if mc_version not in (None, ""):
            mc = normalise_mc_version(mc_version)
            if mc is None:
                return fail(UNSUPPORTED, "%r is not a Minecraft version like 1.21.1 or 26.3." % str(mc_version)[:30])
            params[PARAM["versions_game_versions"]] = json.dumps([mc])
        if loader not in (None, ""):
            norm = normalise_loader(loader)
            if norm is None:
                return fail(UNSUPPORTED, "%r is not a loader Jarvis knows." % str(loader)[:30])
            params[PARAM["versions_loaders"]] = json.dumps([norm])
        got = _get("/project/%s/version" % quote(text, safe=""), params)
        if not got.get("ok"):
            return got
        if not isinstance(got.get("body"), list):
            return fail(VENDOR_CHANGED, "Modrinth's file list is not in the shape Jarvis expects.")
        return {"ok": True, "files": _files_from(got["body"])}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth file list could not be read.")


def _same_project(wanted, actual):
    """True when the version belongs to `wanted` (an id or a slug; empty means 'do not check')."""
    wanted, actual = str(wanted or ""), str(actual or "")
    if not wanted or wanted == actual:
        return True
    if _ID_RE.match(wanted):
        return False                                    # an id that is not this version's project
    found = get_project(wanted)                         # a slug: compare through its id (cached)
    return bool(found.get("ok") and found["project"]["id"] == actual)


def get_file(project_id, file_id):
    """file_id is a Modrinth VERSION id -> {"ok": True, "file": file} | failure"""
    try:
        fid = str(file_id or "").strip()
        if not _SLUG_RE.match(fid):
            return _bad_ref(file_id)
        got = _get("/version/%s" % quote(fid, safe=""))
        if not got.get("ok"):
            return got
        body = got.get("body")
        file = _file(body)
        if file is None:
            return fail(NOT_FOUND, "That Modrinth version has no downloadable file.")
        if not _same_project(project_id, file["project_id"]):
            return fail(NOT_FOUND, "That file belongs to a different project.")
        return {"ok": True, "file": file}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth file could not be read.")


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def bulk_projects(refs):
    """refs = ids or slugs -> {"ok": True, "projects": {ref: project | None}}"""
    try:
        wanted = []
        for ref in refs or []:
            text = str(ref or "").strip()
            if text and text not in wanted:
                wanted.append(text)
        found = {}
        for chunk in _chunks([r for r in wanted if _SLUG_RE.match(r)], BATCH):
            got = _get("/projects", {PARAM["bulk_ids"]: json.dumps(chunk, separators=(",", ":"))})
            if not got.get("ok"):
                return got
            if not isinstance(got.get("body"), list):
                return fail(VENDOR_CHANGED, "Modrinth's project list is not in the shape Jarvis expects.")
            for obj in got["body"]:
                project = _project(obj)
                if project:
                    found[project["id"]] = project
                    if project["slug"]:
                        found[project["slug"].lower()] = project
        return {"ok": True, "projects": {r: found.get(r) or found.get(r.lower()) for r in wanted}}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth project list could not be read.")


def bulk_list_files(project_ids, mc_version=None, loader=None):
    """-> {"ok": True, "files": {project_id: [file]}}.  Modrinth cannot filter versions in bulk, so this is one
    filtered request per project (cached, a few at a time); a project whose request failed is listed with [] and
    named in `errors` ({project_id: reason}).  When every request failed alike, that failure is returned."""
    try:
        ids = []
        for pid in project_ids or []:
            text = str(pid or "").strip()
            if text and text not in ids:
                ids.append(text)
        if not ids:
            return {"ok": True, "files": {}}
        workers = max(1, min(WORKERS, len(ids)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            replies = list(pool.map(lambda p: list_files(p, mc_version, loader), ids))
        files, errors = {}, {}
        for pid, reply in zip(ids, replies):
            if reply.get("ok"):
                files[pid] = reply["files"]
            else:
                files[pid] = []
                errors[pid] = reply.get("reason") or VENDOR_CHANGED
        if errors and len(errors) == len(ids):
            first = replies[0]
            if len({r.get("reason") for r in replies}) == 1 and first.get("reason") in (OFFLINE, RATE_LIMITED):
                return first
        out = {"ok": True, "files": files}
        if errors:
            out["errors"] = errors
        return out
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth file lists could not be read.")


def bulk_get_files(pairs):
    """pairs = [(project_id, version_id)] -> {"ok": True, "files": {"<project_id>:<file_id>": file | None}}"""
    try:
        wanted = []
        for pair in pairs or []:
            try:
                pid, fid = str(pair[0] or ""), str(pair[1] or "")
            except (TypeError, IndexError):
                continue
            if fid and (pid, fid) not in wanted:
                wanted.append((pid, fid))
        by_id = {}
        ids = []
        for _pid, fid in wanted:
            if _SLUG_RE.match(fid) and fid not in ids:
                ids.append(fid)
        for chunk in _chunks(ids, BATCH):
            got = _get("/versions", {PARAM["bulk_ids"]: json.dumps(chunk, separators=(",", ":"))})
            if not got.get("ok"):
                return got
            if not isinstance(got.get("body"), list):
                return fail(VENDOR_CHANGED, "Modrinth's version list is not in the shape Jarvis expects.")
            for version in got["body"]:
                file = _file(version)
                if file:
                    by_id[file["file_id"]] = file
        out = {}
        for pid, fid in wanted:
            file = by_id.get(fid)
            out["%s:%s" % (pid, fid)] = file if file and _same_project(pid, file["project_id"]) else None
        return {"ok": True, "files": out}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth versions could not be read.")


# =====================================================================================================
# Additions for cross-platform matching (agent S1).  Nothing above this line was changed; crossmatch.py
# is the only caller.  One new function, kept apart from the contract's seven.
#
#   POST /version_files  {"hashes": [..], "algorithm": "sha1" | "sha512"}  -> an object keyed by hash, each value
#   a version.  VERIFIED by the owner's probe (25 Oct 2026, tests/data/modhosts/modrinth_version_files.json).
#   What the probe did NOT show: how many hashes one request may carry (HASH_BATCH is a cautious guess; Modrinth
#   states no number) and what a version with several files looks like in the answer (the code below does not
#   assume the matched file is the primary one: it picks the file whose own hash matched and says whether that
#   file is the version's main file).
# =====================================================================================================
HASH_BATCH = 100
_HASH_PATTERN = {"sha1": re.compile(r"^[0-9a-f]{40}$"), "sha512": re.compile(r"^[0-9a-f]{128}$")}


def _file_with_hash(version, algorithm, wanted):
    """The contract file record of the ONE file of `version` whose `algorithm` hash is `wanted`, and whether that
    file is the version's main (primary) file -> (file, is_primary) | None.  `_file` builds the record from a
    version's primary file, so the version is handed to it with only the matching file left (marked primary)."""
    if not isinstance(version, dict):
        return None
    files = [f for f in version.get("files") or [] if isinstance(f, dict)]
    if not files:
        return None
    main = next((f for f in files if f.get("primary")), files[0])
    for one in files:
        hashes = one.get("hashes") if isinstance(one.get("hashes"), dict) else {}
        if str(hashes.get(algorithm) or "").lower() == wanted:
            shown = dict(one)
            shown["primary"] = True
            narrowed = dict(version)
            narrowed["files"] = [shown]
            record = _file(narrowed)
            return (record, one is main) if record and record["file_id"] else None
    return None


def versions_by_hashes(hashes, algorithm="sha1"):
    """Which Modrinth files have exactly these hashes?  `hashes` are hex strings (sha1 by default).

    -> {"ok": True, "files": {hash: file}, "primary": {hash: bool}} | failure.
    A hash Modrinth does not know is simply absent.  `file` is the contract's file record of the file that matched
    (its `file_id` is the Modrinth VERSION id, its `project_id` the project); `primary` says whether that file is the
    version's main file (a pack lists a version's main file only, so a caller must not use a secondary one).
    One POST per HASH_BATCH hashes; nothing is cached (a POST)."""
    try:
        algo = str(algorithm or "sha1").strip().lower()
        pattern = _HASH_PATTERN.get(algo)
        if pattern is None:
            return fail(UNSUPPORTED, "Modrinth looks files up by sha1 or sha512, not %r." % str(algorithm)[:20])
        wanted = []
        for raw in hashes or []:
            text = str(raw or "").strip().lower()
            if pattern.match(text) and text not in wanted:
                wanted.append(text)
        files, primary = {}, {}
        for chunk in _chunks(wanted, HASH_BATCH):
            reply = net.fetch(VENDOR, BASE + "/version_files", method="POST",
                              json_body={"hashes": chunk, "algorithm": algo})
            if not reply.get("ok"):
                return reply
            body = reply.get("body")
            if not isinstance(body, dict):
                return fail(VENDOR_CHANGED, "Modrinth's hash lookup is not in the shape Jarvis expects.")
            asked = set(chunk)
            for key, version in body.items():
                text = str(key or "").lower()
                if text not in asked:
                    continue                                    # only ever what was asked for
                found = _file_with_hash(version, algo, text)
                if found:
                    files[text], primary[text] = found
        return {"ok": True, "files": files, "primary": primary}
    except Exception:  # noqa: BLE001
        return fail(VENDOR_CHANGED, "The Modrinth hash lookup could not be read.")
