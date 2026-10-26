"""Integration test for the Minecraft mod-source tools, written by agent P7.

The other tests of this group replace the layer under test's neighbours with fakes (the download tool's test fakes
the adapters; the adapters' tests fake `net.fetch`).  This one fakes ONLY the lowest layer, the transport, so the
real code runs end to end:

    tool handler -> resolve -> real Modrinth / CurseForge adapter -> real modhosts/net.py (allow-list, SSRF check,
    key header, redirects, scrubbing) -> fake transport (inline vendor answers)

and for a download the real Download Hub `_queue()` stores a real row (no transfer: JARVIS_HUB_NO_DELIVER=1).

Scenarios:
  1. CurseForge download: queued with its sha1; the key header went to api.curseforge.com only; the key is on no
     row and in no result; the `download-url` route was never asked.
  2. A CurseForge file whose author blocked distribution (null link): refused, no row, no `download-url` call.
  3. Modrinth download, then a Modrinth pack of the same project: the checksum the download row verifies is the
     checksum the .mrpack index lists; the required dependency is added; nothing outside the index and overrides
     is in the archive.
  4. CurseForge pack: the blocked project is skipped and reported, the other is in manifest.json by id; the key is
     in no byte of the archive.
  5. Dry runs write nothing and queue nothing; no request went to a loader-meta or OptiFine host (the pack is
     given a loader_version, so none is needed).

Everything is inline: this file reads nothing from tests/data/modhosts.  (The fixture prefix hand_p7_ is reserved
for this agent's files if one is ever needed.)

Run: python3 tests/test_minecraft_integration.py   (plain script, prints "ok" / "FAILED", exits 1 on any failure)
"""
import hashlib
import json
import os
import socket
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HOME = tempfile.mkdtemp(prefix="jarvis-minecraft-integration-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER",
             "JARVIS_TOOL_MAKER_CONTEXT_FILE", "CURSEFORGE_API_KEY"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import ui_bridge  # noqa: E402
from jarvis.actions import minecraft_download as dl  # noqa: E402
from jarvis.actions import minecraft_packs as mp  # noqa: E402
from jarvis.hub import service  # noqa: E402
from jarvis.modhosts import cache, config, net  # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-P7-8a41d6c0e9b35f27"
LIB = Path(HOME) / "library"
_n = [0]

# The hub refuses a link whose host does not resolve to a public address; this file never touches DNS.
socket.getaddrinfo = lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
service._store(None).set_settings({"library_root": str(LIB)})


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


def h(algo, seed):
    return hashlib.new(algo, seed.encode()).hexdigest()


def fresh(label):
    _n[0] += 1
    return str(Path(HOME) / "mc" / ("%s-%d" % (label, _n[0])))


# ------------------------------------------------------------------ the vendors' answers (inline)
SOD_SHA1, SOD_SHA512 = h("sha1", "sodium"), h("sha512", "sodium")
FAB_SHA1, FAB_SHA512 = h("sha1", "fabric-api"), h("sha512", "fabric-api")
CF_SHA1 = h("sha1", "cf-5001")
SOD_URL = "https://cdn.modrinth.com/data/AANobbMI/versions/vSod0001/sodium-fabric-0.5.3+mc1.20.1.jar"
FAB_URL = "https://cdn.modrinth.com/data/P7dR8mSH/versions/vFab0001/fabric-api-0.92.0+1.20.1.jar"
CF_URL = "https://edge.forgecdn.net/files/5/001/cf-mod-1.0.jar"

MODRINTH_PROJECTS = {
    "AANobbMI": {"id": "AANobbMI", "slug": "sodium", "project_type": "mod", "title": "Sodium",
                 "description": "A rendering engine replacement.", "categories": ["optimization"],
                 "loaders": ["fabric"], "game_versions": ["1.20.1"], "client_side": "required",
                 "server_side": "unsupported", "downloads": 10, "updated": "2026-10-01T00:00:00Z"},
    "P7dR8mSH": {"id": "P7dR8mSH", "slug": "fabric-api", "project_type": "mod", "title": "Fabric API",
                 "description": "The Fabric library mod.", "categories": ["library"],
                 "loaders": ["fabric"], "game_versions": ["1.20.1"], "client_side": "required",
                 "server_side": "required", "downloads": 20, "updated": "2026-10-02T00:00:00Z"},
}
MODRINTH_VERSIONS = {
    "AANobbMI": [{
        "id": "vSod0001", "project_id": "AANobbMI", "name": "Sodium 0.5.3", "version_number": "0.5.3",
        "game_versions": ["1.20.1"], "loaders": ["fabric"], "version_type": "release", "status": "listed",
        "date_published": "2026-09-01T00:00:00Z",
        "files": [{"hashes": {"sha1": SOD_SHA1, "sha512": SOD_SHA512}, "url": SOD_URL,
                   "filename": "sodium-fabric-0.5.3+mc1.20.1.jar", "primary": True, "size": 1000}],
        "dependencies": [{"project_id": "P7dR8mSH", "version_id": None, "dependency_type": "required"}]}],
    "P7dR8mSH": [{
        "id": "vFab0001", "project_id": "P7dR8mSH", "name": "Fabric API 0.92.0", "version_number": "0.92.0",
        "game_versions": ["1.20.1"], "loaders": ["fabric"], "version_type": "release", "status": "listed",
        "date_published": "2026-09-02T00:00:00Z",
        "files": [{"hashes": {"sha1": FAB_SHA1, "sha512": FAB_SHA512}, "url": FAB_URL,
                   "filename": "fabric-api-0.92.0+1.20.1.jar", "primary": True, "size": 2000}],
        "dependencies": []}],
}


def cf_mod(mod_id, slug, name, allow):
    return {"id": mod_id, "slug": slug, "name": name, "summary": "A mod.", "classId": 6,
            "allowModDistribution": allow, "downloadCount": 5, "dateModified": "2026-10-01T00:00:00Z",
            "authors": [{"name": "someone"}], "categories": [], "links": {},
            "latestFilesIndexes": [{"gameVersion": "1.20.1", "modLoader": 4}], "latestFiles": []}


def cf_file(file_id, mod_id, name, url, sha1):
    return {"id": file_id, "modId": mod_id, "fileName": name, "displayName": name, "gameVersions": ["1.20.1", "Fabric"],
            "releaseType": 1, "fileDate": "2026-10-01T00:00:00Z", "fileLength": 1234, "isAvailable": True,
            "downloadUrl": url, "hashes": [{"algo": 1, "value": sha1}], "dependencies": []}


CF_MODS = {1001: cf_mod(1001, "cf-mod", "Cf Mod", True), 1002: cf_mod(1002, "blocked-mod", "Blocked Mod", False)}
CF_FILES = {1001: [cf_file(5001, 1001, "cf-mod-1.0.jar", CF_URL, CF_SHA1)],
            1002: [cf_file(5002, 1002, "blocked-mod-1.0.jar", None, h("sha1", "cf-5002"))]}


class World:
    """net.TRANSPORT.  Every request is recorded: method, host, path, whether the key header was on it."""

    def __init__(self):
        self.calls = []

    def paths(self, host=None):
        return [c["path"] for c in self.calls if host is None or c["host"] == host]

    def transport(self, method, url, headers, json_body, timeout):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        self.calls.append({"method": method, "host": parts.hostname, "path": parts.path,
                           "has_key": any(str(k).lower() == "x-api-key" for k in (headers or {}))})
        if parts.hostname == "api.modrinth.com":
            data = self._modrinth(parts.path, query)
        elif parts.hostname == "api.curseforge.com":
            data = self._curseforge(method, parts.path, json_body)
        else:
            data = None
        if data is None:
            return net.Reply(404, {"content-type": "application/json"}, b'{"error": "not_found"}')
        return net.Reply(200, {"content-type": "application/json"}, json.dumps(data).encode())

    @staticmethod
    def _find(ref):
        ref = unquote(ref).lower()
        for pid, project in MODRINTH_PROJECTS.items():
            if ref in (pid.lower(), project["slug"]):
                return pid
        return None

    def _modrinth(self, path, query):
        path = path[len("/v2"):] if path.startswith("/v2") else path
        if path == "/projects":
            wanted = json.loads((query.get("ids") or ["[]"])[0])
            return [MODRINTH_PROJECTS[p] for p in (self._find(w) for w in wanted) if p]
        if path == "/versions":
            wanted = json.loads((query.get("ids") or ["[]"])[0])
            return [v for vs in MODRINTH_VERSIONS.values() for v in vs if v["id"] in wanted]
        if path.startswith("/version/"):
            vid = unquote(path.split("/")[2])
            return next((v for vs in MODRINTH_VERSIONS.values() for v in vs if v["id"] == vid), None)
        if path.startswith("/project/"):
            bits = path.split("/")
            pid = self._find(bits[2])
            if pid is None:
                return None
            if len(bits) == 3:
                return MODRINTH_PROJECTS[pid]
            if bits[3] == "version":
                return MODRINTH_VERSIONS.get(pid, [])
            if bits[3] == "members":
                return [{"user": {"username": "alice"}, "role": "Owner"}]
        return None

    @staticmethod
    def _curseforge(method, path, json_body):
        page = {"index": 0, "pageSize": 50, "resultCount": 1, "totalCount": 1}
        path = path[len("/v1"):] if path.startswith("/v1") else path
        bits = [b for b in path.split("/") if b]
        if bits == ["mods"] and method == "POST":
            ids = (json_body or {}).get("modIds") or []
            return {"data": [CF_MODS[i] for i in ids if i in CF_MODS]}
        if len(bits) == 2 and bits[0] == "mods" and bits[1].isdigit():
            return {"data": CF_MODS[int(bits[1])]} if int(bits[1]) in CF_MODS else None
        if len(bits) == 3 and bits[0] == "mods" and bits[2] == "files" and bits[1].isdigit():
            files = CF_FILES.get(int(bits[1]), [])
            return {"data": files, "pagination": dict(page, resultCount=len(files), totalCount=len(files))}
        if len(bits) == 4 and bits[2] == "files" and bits[1].isdigit() and bits[3].isdigit():
            for f in CF_FILES.get(int(bits[1]), []):
                if f["id"] == int(bits[3]):
                    return {"data": f}
        return None


class Ask:
    """ui_bridge.confirm answers yes (the owner agrees to a folder outside the library); every question is recorded."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        self.real = ui_bridge.confirm

        def fake(question, body="", **kw):
            self.calls.append({"title": question, "body": body})
            return True
        ui_bridge.confirm = fake
        return self

    def __exit__(self, *exc):
        ui_bridge.confirm = self.real


def fresh_world():
    world = World()
    net.TRANSPORT = world.transport
    net.RESOLVER = lambda host, port: ["93.184.216.34"]
    net.SLEEP = lambda seconds: None
    folder = Path(HOME) / ".jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / config.CONFIG_NAME).write_text(json.dumps({"api_key": FAKE_KEY}), encoding="utf-8")
    cache.clear()
    return world


def all_rows():
    return service._store(None).list_items(limit=5000, include_disabled=True, include_dismissed=True)


def row(item_id):
    return service._store(None).get_item(item_id) or {}


def text_of(value):
    return json.dumps(value, ensure_ascii=False, default=str)


FAB = {"mc_version": "1.20.1", "loader": "fabric"}


def make_pack(**args):
    base = {"name": "p7 pack", "mc_version": "1.20.1", "loader": "fabric", "loader_version": "0.15.11"}
    base.update(args)
    return mp.tool_make_modpack_file(base)


def files_in(folder):
    p = Path(folder)
    return sorted(x.name for x in p.iterdir()) if p.exists() else []


# =====================================================================================================================
def test_1_curseforge_download_end_to_end():
    world = fresh_world()
    dest = fresh("cf")
    with Ask():
        n = len(all_rows())
        res = dl.tool_download_mc_file(dict(project="1001", source="curseforge", dest=dest, **FAB))
    check("CurseForge: queued", res.get("ok") is True and res.get("queued") is True, res)
    ids = (res.get("hub") or {}).get("ids") or []
    check("CurseForge: exactly one new row", len(ids) == 1 and len(all_rows()) == n + 1, (ids, len(all_rows()) - n))
    stored = row(ids[0]) if ids else {}
    verify = (stored.get("options") or {}).get("verify") or {}
    check("CurseForge: the row verifies with the vendor's sha1", verify.get("algo") == "sha1"
          and verify.get("value") == CF_SHA1, verify)
    check("CurseForge: the result says the checksum is applied", (res.get("verify") or {}).get("applied") is True, res.get("verify"))
    check("CurseForge: the link on the row is on the CDN host", "edge.forgecdn.net" in text_of(stored), text_of(stored)[:200])
    check("CurseForge: the message says not installed", "not installed" in str(res.get("message", "")), res.get("message"))
    check("CurseForge: the key is in no result and on no row", FAKE_KEY not in text_of(res) and FAKE_KEY not in text_of(stored))
    keyed = sorted({c["host"] for c in world.calls if c["has_key"]})
    check("CurseForge: the key header went to api.curseforge.com only", keyed == ["api.curseforge.com"], keyed)
    check("CurseForge: the download-url route was never asked", not any(p.endswith("/download-url") for p in world.paths()),
          world.paths())
    check("CurseForge: nothing was fetched from the CDN by the tool (the hub does that later)",
          not any(c["host"] == "edge.forgecdn.net" for c in world.calls))


def test_2_blocked_curseforge_file_is_refused_not_worked_around():
    world = fresh_world()
    with Ask():
        n = len(all_rows())
        res = dl.tool_download_mc_file(dict(project="1002", source="curseforge", dest=fresh("blocked"), **FAB))
    check("blocked: not queued", res.get("ok") is not True or res.get("queued") is not True, res)
    check("blocked: no row appeared", len(all_rows()) == n, len(all_rows()) - n)
    check("blocked: the download-url route was never asked (an author's choice is not worked around)",
          not any(p.endswith("/download-url") for p in world.paths()), world.paths())
    check("blocked: nothing was asked of a CDN host", not any(c["host"] == "edge.forgecdn.net" for c in world.calls))


def test_3_modrinth_download_then_pack_agree():
    world = fresh_world()
    dest = fresh("mr-dl")
    with Ask():
        res = dl.tool_download_mc_file(dict(project="sodium", source="modrinth", dest=dest, **FAB))
    check("Modrinth: queued", res.get("ok") is True and res.get("queued") is True, res)
    ids = (res.get("hub") or {}).get("ids") or []
    verify = ((row(ids[0]) if ids else {}).get("options") or {}).get("verify") or {}
    check("Modrinth: the row verifies with sha512", verify.get("algo") == "sha512" and verify.get("value") == SOD_SHA512, verify)
    check("Modrinth: the key header was sent to no host (Modrinth needs none)", not any(c["has_key"] for c in world.calls),
          [c for c in world.calls if c["has_key"]][:2])

    out = fresh("mr-pack")
    with Ask():
        dry = make_pack(format="modrinth", entries=[{"project": "sodium"}], dest=out, dry_run=True)
    check("pack dry run: ok", dry.get("ok") is True and dry.get("dry_run") is True, dry)
    check("pack dry run: the exact file is the one the download picked", "vSod0001" in text_of(dry), text_of(dry)[:300])
    check("pack dry run: the required dependency is added", "P7dR8mSH" in text_of(dry), text_of(dry)[:300])
    check("pack dry run: nothing was written", files_in(out) == [], files_in(out))

    with Ask():
        res = make_pack(format="modrinth", entries=[{"project": "sodium"}], dest=out)
    check("pack: written", res.get("ok") is True, res)
    packs = [n for n in files_in(out) if n.endswith(".mrpack")]
    check("pack: one .mrpack in the folder and nothing else", len(packs) == 1 and len(files_in(out)) == 1, files_in(out))
    if not packs:
        return
    with zipfile.ZipFile(Path(out) / packs[0]) as z:
        names = z.namelist()
        index = json.loads(z.read("modrinth.index.json").decode("utf-8"))
        blob = b"".join(z.read(n) for n in names)
    check("pack: the archive holds only the index and overrides", all(n == "modrinth.index.json" or n.startswith("overrides/")
                                                                       for n in names), names)
    entries = {Path(f.get("path", "")).name: f for f in index.get("files", [])}
    sodium = entries.get("sodium-fabric-0.5.3+mc1.20.1.jar") or {}
    check("pack: the index lists the Sodium file with the same sha512 the download row verifies",
          (sodium.get("hashes") or {}).get("sha512") == SOD_SHA512 == verify.get("value"), sodium)
    check("pack: and the same sha1", (sodium.get("hashes") or {}).get("sha1") == SOD_SHA1, sodium)
    check("pack: the download link is the vendor's own CDN link", sodium.get("downloads") == [SOD_URL], sodium.get("downloads"))
    check("pack: the dependency file is listed too", "fabric-api-0.92.0+1.20.1.jar" in entries, sorted(entries))
    deps = index.get("dependencies") or {}
    check("pack: the dependencies name the Minecraft version and the given loader version",
          deps.get("minecraft") == "1.20.1" and deps.get("fabric-loader") == "0.15.11", deps)
    check("pack: the key is in no byte of the archive", FAKE_KEY.encode() not in blob)
    check("pack: no request went to a loader or OptiFine host", all(c["host"] in ("api.modrinth.com", "api.curseforge.com")
                                                                    for c in world.calls), sorted({c["host"] for c in world.calls}))


def test_4_curseforge_pack_skips_the_blocked_project():
    world = fresh_world()
    out = fresh("cf-pack")
    with Ask():
        dry = make_pack(format="curseforge", entries=[{"project": "1001"}, {"project": "1002"}], dest=out, dry_run=True)
    check("CurseForge pack dry run: ok", dry.get("ok") is True and dry.get("dry_run") is True, dry)
    skipped = text_of(dry.get("skipped"))
    check("CurseForge pack dry run: the blocked project is reported as skipped", "1002" in skipped or "Blocked Mod" in skipped, dry)
    check("CurseForge pack dry run: nothing was written", files_in(out) == [], files_in(out))
    with Ask():
        res = make_pack(format="curseforge", entries=[{"project": "1001"}, {"project": "1002"}], dest=out)
    check("CurseForge pack: written", res.get("ok") is True, res)
    zips = [n for n in files_in(out) if n.endswith(".zip")]
    check("CurseForge pack: one .zip in the folder", len(zips) == 1, files_in(out))
    if not zips:
        return
    with zipfile.ZipFile(Path(out) / zips[0]) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        blob = b"".join(z.read(n) for n in names)
    listed = {(f.get("projectID"), f.get("fileID")) for f in manifest.get("files", [])}
    check("CurseForge pack: the open project is in the manifest by project and file id", (1001, 5001) in listed, listed)
    check("CurseForge pack: the blocked project is not", not any(p == 1002 for p, _ in listed), listed)
    check("CurseForge pack: the manifest names the Minecraft version", "1.20.1" in json.dumps(manifest.get("minecraft")), manifest.get("minecraft"))
    check("CurseForge pack: the key is in no byte of the archive", FAKE_KEY.encode() not in blob)
    check("CurseForge pack: the download-url route was never asked", not any(p.endswith("/download-url") for p in world.paths()))
    check("CurseForge pack: the key header went to api.curseforge.com only",
          sorted({c["host"] for c in world.calls if c["has_key"]}) == ["api.curseforge.com"])


def test_5_dry_runs_leave_nothing_behind():
    fresh_world()
    dest = fresh("dry")
    with Ask() as ask:
        n = len(all_rows())
        a = dl.tool_download_mc_file(dict(project="sodium", source="modrinth", dest=dest, dry_run=True, **FAB))
        b = make_pack(format="modrinth", entries=[{"project": "sodium"}], dest=dest, dry_run=True)
    check("download dry run: ok and not queued", a.get("ok") is True and a.get("queued") is False, a)
    check("pack dry run: ok", b.get("ok") is True, b)
    check("dry runs: no row and no file were made", len(all_rows()) == n and files_in(dest) == [],
          (len(all_rows()) - n, files_in(dest)))
    check("dry runs: the owner was asked nothing", ask.calls == [], ask.calls)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        print("\n== %s" % test.__name__)
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s ran without an exception" % test.__name__, False, repr(exc))
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAILED: %s -> %s" % (name, detail))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
