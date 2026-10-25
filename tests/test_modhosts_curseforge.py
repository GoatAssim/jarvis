"""P3: the CurseForge source adapter (jarvis/modhosts/curseforge.py), against a fake `fetch`.

No network, no sockets, no real key. `HOME` is redirected before any jarvis import. The fake
(tests/_modhosts_fake_curseforge.py) serves hand-written fixtures built from the SHAPES the owner's
probe recorded (tests/data/modhosts/hand_p3_*.json); none of this is a recorded vendor answer.

Pinned here:
  1. gameVersions is classified, never read by position; Minecraft versions of both shapes (1.21.11, 26.3);
  2. every adapter function answers "no_key" with the how-to-get-one sentence and makes NO request
     when there is no usable key (missing, damaged); the env var works as the fallback;
  3. search / get_project (id, slug, ambiguous, unsupported class) / list_files / get_file / bulk_*
     return contract records, with exact mc_version and loader filters that re-check each file's own
     tags (the vendor's loader filter is loose), server packs left out, newest first, paging;
  4. a project that forbids third-party distribution: null downloadUrl -> downloadable False, never
     worked around; the download-url route's 403 -> not_distributable; unknown CDN hosts are refused;
  5. only SHA-1 is exposed as a checksum; dependency relation types map onto the contract's four;
  6. vendor text is stripped, flattened and clipped; page links are checked;
  7. errors in every shape `net.fetch` might use (failure dict with or without status, ok with a 4xx
     status) become the contract's reason codes, nothing raises, and the key never appears in a result,
     an error, a recorded call or a header;
  8. static: the adapter opens no connection itself, never names the key header or file, and has the
     contract's signatures (a trailing keyword-only `fetch=None`).

Run: python3 tests/test_modhosts_curseforge.py
"""

import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-p3-cf-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("CURSEFORGE_API_KEY", None)

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(_TESTS.parent / "jarvis-cli"))

import _modhosts_fake_curseforge as fakes  # noqa: E402
from jarvis.modhosts import config, curseforge as cf  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"
KEY_FILE = JDIR / "curseforge.json"
FAKE_KEY = "cf-test-FAKE-KEY-not-a-real-key-0123456789"     # obviously fake; asserted absent everywhere
CONTRACT_REASONS = {"no_key", "bad_key", "rate_limited", "not_found", "ambiguous", "not_distributable",
                    "no_compatible_file", "no_loader_version", "bad_dest", "exists", "too_big", "verify_failed",
                    "vendor_changed", "offline", "unsupported"}
PROJECT_KEYS = {"source", "id", "slug", "name", "summary", "author", "downloads", "updated", "kind", "mc_versions",
                "loaders", "page_url", "distributable", "client_side", "server_side"}
FILE_KEYS = {"source", "project_id", "file_id", "file_name", "version_label", "mc_versions", "loaders", "channel",
             "date", "size", "download_url", "downloadable", "hashes", "dependencies"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _key(value=FAKE_KEY):
    JDIR.mkdir(parents=True, exist_ok=True)
    if value is None:
        KEY_FILE.unlink(missing_ok=True)
    else:
        KEY_FILE.write_text(json.dumps({"api_key": value}), encoding="utf-8")
    os.environ.pop("CURSEFORGE_API_KEY", None)


def _fake(style="failure"):
    _key()
    return fakes.FakeCurseForge(style=style)


def _ids(files):
    return [f["file_id"] for f in files]


def _dump(*things):
    return json.dumps(things, default=str)


# ---- 1. gameVersions ------------------------------------------------------------------------
def test_classify_game_versions():
    got = cf.classify_game_versions(["Client", "Fabric", "Quilt", "26.2", "26.3"])
    check("minecraft versions, loaders and tags are told apart wherever they sit",
          got == {"minecraft": ["26.3", "26.2"], "loaders": ["fabric", "quilt"], "other": ["Client"]}, got)
    got = cf.classify_game_versions(["1.21.9", "1.21.11", "1.21.10", "26.1.2", "26.1", "NeoForge", "Forge", "Java 21", "1.21-Snapshot", "Server", 7, None])
    check("versions sort by number, newest first, both shapes (1.21.11 beats 1.21.9, 26.x beats 1.x)",
          got["minecraft"] == ["26.1.2", "26.1", "1.21.11", "1.21.10", "1.21.9"], got)
    check("NeoForge is not Forge; snapshots and Java tags are 'other'; non-text entries are ignored",
          got["loaders"] == ["forge", "neoforge"] and got["other"] == ["Java 21", "1.21-Snapshot", "Server"], got)
    check("a loader name matches case-insensitively", cf.classify_game_versions(["FABRIC"])["loaders"] == ["fabric"])
    check("not a list gives empty parts", cf.classify_game_versions(None) == {"minecraft": [], "loaders": [], "other": []})


# ---- 2. no key ------------------------------------------------------------------------------
def _every_function(fake):
    return {
        "search": lambda: cf.search("light", fetch=fake),
        "get_project": lambda: cf.get_project(900100, fetch=fake),
        "list_files": lambda: cf.list_files(900100, fetch=fake),
        "get_file": lambda: cf.get_file(900100, 9001004, fetch=fake),
        "bulk_projects": lambda: cf.bulk_projects([900100], fetch=fake),
        "bulk_list_files": lambda: cf.bulk_list_files([900100], fetch=fake),
        "bulk_get_files": lambda: cf.bulk_get_files([(900100, 9001004)], fetch=fake),
        "download_url": lambda: cf.download_url(900100, 9001004, fetch=fake),
        "ping": lambda: cf.ping(fetch=fake),
    }


def test_no_key():
    for label, setup in (("no file", lambda: _key(None)),
                         ("an empty key", lambda: _key("")),
                         ("a damaged key (spaces)", lambda: _key("not a valid key"))):
        setup()
        fake = fakes.FakeCurseForge()
        bad = []
        for name, call in _every_function(fake).items():
            r = call()
            if not (r.get("ok") is False and r.get("reason") == "no_key" and "console.curseforge.com" in r.get("error", "")
                    and "Modrinth" in r["error"]):
                bad.append((name, r))
        check(f"{label}: every function answers no_key with the how-to-get-one sentence", not bad, bad[:2])
        check(f"{label}: and no request is made", fake.calls == [], fake.calls)
    _key("not a valid key")
    r = cf.search("x", fetch=fakes.FakeCurseForge())
    check("a damaged key is named as damaged, without its text", "not a valid key" not in _dump(r) and "curseforge.json" in r["error"], r)
    _key(None)
    os.environ["CURSEFORGE_API_KEY"] = FAKE_KEY
    fake = fakes.FakeCurseForge()
    r = cf.search("light", fetch=fake)
    check("the CURSEFORGE_API_KEY variable is the fallback", r.get("ok") is True and len(fake.calls) == 1, r)
    os.environ.pop("CURSEFORGE_API_KEY", None)


# ---- 3. search ------------------------------------------------------------------------------
def test_search():
    fake = _fake()
    r = cf.search("", limit=10, fetch=fake)
    check("search answers hits and a total", r["ok"] and [h["id"] for h in r["hits"]] == ["900100", "900101", "900102", "900104", "900120", "900108"] and r["total"] == 6, r)
    c = fake.calls[0]
    check("it asks the right route, vendor and numbers (game 432, class 6, Popularity, desc)",
          c["vendor"] == "curseforge" and c["url"] == "https://api.curseforge.com/v1/mods/search"
          and c["params"]["gameId"] == 432 and c["params"]["classId"] == 6 and c["params"]["sortField"] == 2
          and c["params"]["sortOrder"] == "desc" and c["params"]["pageSize"] == 10 and c["params"]["index"] == 0
          and "searchFilter" not in c["params"], c)
    check("a search carries no headers (the key is net.py's job)", all(x["headers"] is None for x in fake.calls))
    for hit in r["hits"]:
        check("hit %s has every contract key and source curseforge" % hit["id"], PROJECT_KEYS <= set(hit) and hit["source"] == "curseforge", set(PROJECT_KEYS) - set(hit))
    fake = _fake()
    r = cf.search("light", mc_version="26.3", loader="fabric", sort="downloads", limit=999, offset=5, fetch=fake)
    p = fake.calls[0]["params"]
    check("filters become gameVersion and modLoaderType 4; limit is clamped to 50; offset passes",
          p["gameVersion"] == "26.3" and p["modLoaderType"] == 4 and p["pageSize"] == 50 and p["index"] == 5 and p["searchFilter"] == "light" and p["sortField"] == 6, p)
    r = cf.search("", loader="quilt", fetch=fake)
    check("quilt is modLoaderType 5 (the swagger number, not the probe's guess)", fake.calls[-1]["params"]["modLoaderType"] == 5)
    cf.search("", loader="neoforge", fetch=fake)
    cf.search("", loader="forge", fetch=fake)
    check("neoforge is 6, forge is 1", [c["params"]["modLoaderType"] for c in fake.calls[-2:]] == [6, 1])
    fake = _fake()
    r = cf.search("", kind="shader", loader="fabric", fetch=fake)
    check("a shader search uses class 6552 and ignores the loader (shaders have none)",
          [h["id"] for h in r["hits"]] == ["900107"] and fake.calls[0]["params"]["classId"] == 6552 and "modLoaderType" not in fake.calls[0]["params"], fake.calls[0])
    for kind, cid in (("modpack", 4471), ("resourcepack", 12), ("datapack", 6945)):
        cf.search("", kind=kind, fetch=fake)
        check(f"kind {kind} is class {cid}", fake.calls[-1]["params"]["classId"] == cid)
    fake = _fake()
    for label, call in (("an unknown kind", lambda: cf.search("x", kind="world", fetch=fake)),
                        ("an unknown loader", lambda: cf.search("x", loader="rift", fetch=fake)),
                        ("a malformed Minecraft version", lambda: cf.search("x", mc_version="1.x", fetch=fake))):
        r = call()
        check(f"{label} is refused as unsupported", r["ok"] is False and r["reason"] == "unsupported", r)
    check("and nothing was requested for them", fake.calls == [])
    fake = _fake()
    r = cf.search("", mc_version="26.3", loader="forge", fetch=fake)
    check("26.3 is accepted as a Minecraft version, and the loader filter narrows", [h["id"] for h in r["hits"]] == ["900101"], r)
    fake = _fake()
    fake.show_all_classes = True
    r = cf.search("", limit=50, fetch=fake)
    check("a class the tools do not handle (a world) never becomes a hit", "900103" not in [h["id"] for h in r["hits"]], [h["id"] for h in r["hits"]])


def test_project_record():
    fake = _fake()
    p = cf.get_project(900100, fetch=fake)["project"]
    check("the project record has the contract keys with the contract types",
          PROJECT_KEYS <= set(p) and isinstance(p["downloads"], int) and p["downloads"] == 123456 and p["kind"] == "mod"
          and p["slug"] == "test-light-engine" and p["author"] == "FakeAuthor" and p["updated"] == "2026-10-20T10:00:00Z", p)
    check("mc_versions are exact strings, newest first, a few (6), snapshots left out",
          p["mc_versions"] == ["26.3", "26.2", "26.1.1", "26.1", "1.21.11", "1.21.10"], p["mc_versions"])
    check("loaders are lower-case names in a fixed order", p["loaders"] == ["fabric", "quilt"], p["loaders"])
    check("CurseForge does not say which side runs it", p["client_side"] == "unknown" and p["server_side"] == "unknown")
    check("distributable mirrors allowModDistribution (true / false / unknown)",
          p["distributable"] is True
          and cf.get_project(900102, fetch=fake)["project"]["distributable"] is False
          and cf.get_project(900108, fetch=fake)["project"]["distributable"] is None)
    check("the page link is the project page", p["page_url"] == "https://www.curseforge.com/minecraft/mc-mods/test-light-engine", p["page_url"])
    check("extras for get_mc_project: categories and https links only",
          p["categories"] == ["Performance", "Utility"] and p["links"]["source"] == "https://example.org/src" and "wiki" not in p["links"], p)
    check("a modpack and a shader get their own kinds",
          cf.get_project(900106, fetch=fake)["project"]["kind"] == "modpack" and cf.get_project(900107, fetch=fake)["project"]["kind"] == "shader")
    check("a quilt-and-fabric project lists both (tags are taken as the vendor gives them)", "quilt" in p["loaders"] and "fabric" in p["loaders"])


def test_untrusted_text():
    fake = _fake()
    p = cf.get_project(900100, fetch=fake)["project"]
    check("a summary loses HTML and markdown (links and pictures), is one line and is clipped to ~160",
          "<" not in p["summary"] and "evil.example" not in p["summary"] and "\n" not in p["summary"] and len(p["summary"]) <= 160
          and p["summary"].startswith("Faster lighting for any world") and p["summary"].endswith("\u2026"), p["summary"])
    odd = cf.get_project(900108, fetch=fake)["project"]
    check("a name loses tags, control characters and line breaks",
          "<" not in odd["name"] and "\x07" not in odd["name"] and "\n" not in odd["name"] and "\r" not in odd["name"] and odd["name"].startswith("Odd"), repr(odd["name"]))
    check("a page link that is not on curseforge.com over https is replaced by a built one",
          odd["page_url"] == "https://www.curseforge.com/minecraft/mc-mods/odd-name", odd["page_url"])
    check("an odd source link (javascript:) is dropped", "source" not in odd["links"] and set(odd["links"]) == {"website"}, odd["links"])
    check("clean_text keeps short text and handles non-text", cf.clean_text("a  b", 10) == "a b" and cf.clean_text(None, 10) == "" and cf.clean_text("x" * 50, 10).endswith("\u2026") and len(cf.clean_text("x" * 50, 10)) == 10)


# ---- get_project ----------------------------------------------------------------------------
def test_get_project():
    fake = _fake()
    r = cf.get_project("900101", fetch=fake)
    check("a numeric string is an id (GET /mods/<id>)", r["ok"] and r["project"]["id"] == "900101" and fake.calls[0]["url"].endswith("/v1/mods/900101"), r)
    fake = _fake()
    r = cf.get_project("test-light-engine", fetch=fake)
    check("a slug is found with the search's slug parameter (one call)", r["ok"] and r["project"]["id"] == "900100" and len(fake.calls) == 1 and fake.calls[0]["params"].get("slug") == "test-light-engine", (r, fake.calls))
    fake = _fake()
    fake.ignore_slug = True
    r = cf.get_project("test-light-engine", fetch=fake)
    check("if the slug parameter finds nothing, a text search is tried and only an EXACT slug counts",
          r["ok"] and r["project"]["id"] == "900100" and len(fake.calls) == 2 and fake.calls[1]["params"].get("searchFilter") == "test-light-engine", (r, fake.calls))
    fake = _fake()
    r = cf.get_project("twin", fetch=fake)
    check("a slug shared by a mod and a modpack is ambiguous, with both candidates (never a guess)",
          r["ok"] is False and r["reason"] == "ambiguous" and sorted(c["id"] for c in r["candidates"]) == ["900104", "900105"], r)
    fake = _fake()
    r = cf.get_project("no-such-thing", fetch=fake)
    check("an unknown slug is not_found", r["ok"] is False and r["reason"] == "not_found", r)
    r = cf.get_project("test-world-map", fetch=_fake())
    check("a slug of an unhandled class (worlds) is unsupported, not not_found", r["reason"] == "unsupported", r)
    r = cf.get_project(900103, fetch=_fake())
    check("an id of an unhandled class is unsupported", r["reason"] == "unsupported", r)
    fake = _fake()
    for ref in ("../etc/passwd", "A B", "", None, [], "x" * 300, 0, -4, True):
        r = cf.get_project(ref, fetch=fake)
        check("a ref that is neither an id nor a slug (%r) is not_found, no request" % (ref if not isinstance(ref, str) else ref[:12]), r["ok"] is False and r["reason"] == "not_found", r)
    check("none of those reached the network", fake.calls == [], fake.calls)
    for style in ("failure", "status", "bare"):
        r = cf.get_project(999999, fetch=_fake(style))
        check(f"an unknown id is not_found in the '{style}' error shape", r["ok"] is False and r["reason"] == "not_found", r)


# ---- list_files -----------------------------------------------------------------------------
def test_list_files():
    fake = _fake()
    r = cf.list_files(900100, fetch=fake)
    ids = _ids(r["files"])
    check("every channel, newest first, the server pack left out",
          ids == ["9001005", "9001004", "9001003", "9001002", "9001007", "9001006", "9001000"] and "9001001" not in ids, ids)
    check("it hits GET /mods/<id>/files with a page size", fake.calls[0]["url"].endswith("/v1/mods/900100/files") and fake.calls[0]["params"]["pageSize"] == 50, fake.calls[0])
    check("every file record has the contract keys", all(FILE_KEYS <= set(f) and f["source"] == "curseforge" and f["project_id"] == "900100" for f in r["files"]))
    check("the adapter never sets `via` (the Quilt fallback is resolve.pick_file's)", all("via" not in f for f in r["files"]))
    by = {f["file_id"]: f for f in r["files"]}
    check("channels: 1 release, 3 alpha, an unknown number is treated as alpha (never preferred)",
          by["9001004"]["channel"] == "release" and by["9001005"]["channel"] == "alpha" and by["9001007"]["channel"] == "alpha")
    r = cf.list_files(900100, mc_version="26.3", fetch=_fake())
    check("mc_version is exact: a 1.21.11 file is not offered for 26.3", "9001002" not in _ids(r["files"]) and "9001004" in _ids(r["files"]), _ids(r["files"]))
    r = cf.list_files(900100, mc_version="26.2", fetch=_fake())
    check("a file listing several versions matches each of them", _ids(r["files"]) == ["9001004"], _ids(r["files"]))
    fake = _fake()
    r = cf.list_files(900100, mc_version="26.3", loader="fabric", fetch=fake)
    check("loader fabric: Fabric-tagged files only (a Quilt-only file is not Fabric)",
          _ids(r["files"]) == ["9001005", "9001004", "9001007", "9001006", "9001000"], _ids(r["files"]))
    check("the request carried gameVersion and modLoaderType 4", fake.calls[0]["params"]["gameVersion"] == "26.3" and fake.calls[0]["params"]["modLoaderType"] == 4)
    r = cf.list_files(900100, mc_version="26.3", loader="quilt", fetch=_fake())
    check("loader quilt: ONLY files tagged Quilt, although the vendor's filter let the Fabric ones through",
          _ids(r["files"]) == ["9001004", "9001003"], _ids(r["files"]))
    r = cf.list_files(900101, mc_version="26.3", loader="forge", fetch=_fake())
    check("loader forge: a NeoForge-only file is not Forge; a file tagged both is", _ids(r["files"]) == ["9101002"], _ids(r["files"]))
    r = cf.list_files(900101, mc_version="26.3", loader="neoforge", fetch=_fake())
    check("loader neoforge: the NeoForge-only and the both-tagged file", _ids(r["files"]) == ["9101003", "9101002"], _ids(r["files"]))
    r = cf.list_files(900100, mc_version="1.21.11", loader="fabric", fetch=_fake())
    check("a 1.x version still works next to the 26.x ones", _ids(r["files"]) == ["9001002"], _ids(r["files"]))
    r = cf.list_files(900100, mc_version="1.21.11", loader="quilt", fetch=_fake())
    check("no match is an empty list, not an error (the caller picks the fallback)", r == {"ok": True, "files": []}, r)
    fake = _fake()
    r = cf.list_files(900120, fetch=fake)
    check("paging: 120 files over three requests (index 0, 50, 100), newest first",
          len(r["files"]) == 120 and [c["params"]["index"] for c in fake.calls] == [0, 50, 100] and r["files"][0]["file_id"] == "9120120", (len(r["files"]), [c["params"]["index"] for c in fake.calls]))
    for style in ("failure", "status", "bare"):
        r = cf.list_files(999999, fetch=_fake(style))
        check(f"an unknown project is not_found ('{style}' shape)", r["ok"] is False and r["reason"] == "not_found", r)
    r = cf.list_files("abc", fetch=fake)
    check("a bad project id is refused without a request", r["reason"] == "not_found")


def test_file_fields():
    fake = _fake()
    f = {x["file_id"]: x for x in cf.list_files(900100, fetch=fake)["files"]}["9001004"]
    check("name, label, size, date, versions and loaders come from the file",
          f["file_name"] == "test-light-engine-0.8.0.jar" and f["version_label"] == "test-light-engine 0.8.0" and f["size"] == 2048
          and f["date"] == "2026-10-10T10:00:00Z" and f["mc_versions"] == ["26.3", "26.2"] and f["loaders"] == ["fabric", "quilt"], f)
    check("only SHA-1 is a checksum: md5 (algo 2) is left out, the fingerprint is kept apart",
          f["hashes"] == {"sha1": "%040x" % 9001004, "fingerprint": str(1000000 + 9001004 % 1000)} and "md5" not in f["hashes"], f["hashes"])
    check("dependencies map onto required / optional / embedded / incompatible, dedupe, and drop junk",
          f["dependencies"] == [{"project_id": "900101", "file_id": None, "type": "required"},
                                {"project_id": "900102", "file_id": None, "type": "optional"},
                                {"project_id": "900109", "file_id": None, "type": "embedded"},
                                {"project_id": "900110", "file_id": None, "type": "incompatible"},
                                {"project_id": "900111", "file_id": None, "type": "embedded"},
                                {"project_id": "900112", "file_id": None, "type": "optional"}], f["dependencies"])
    raw = fake.files[900100][1]
    check("the download link is passed on exactly as the API gave it", f["download_url"] == raw["downloadUrl"] and f["downloadable"] is True and "why" not in f)
    # a hash that is not 40 hex characters is not a checksum
    fake.files[900104][0]["hashes"] = [{"value": "xyz", "algo": 1}, {"value": "A" * 40, "algo": 1}]
    g = cf.get_file(900104, 9104001, fetch=fake)["file"]
    check("a malformed hash is dropped and a valid one is lower-cased", g["hashes"].get("sha1") == "a" * 40, g["hashes"])


# ---- distribution ---------------------------------------------------------------------------
def test_distribution():
    fake = _fake()
    files = cf.list_files(900102, fetch=fake)["files"]
    check("a project that forbids distribution: every file has a null link, downloadable False, download_url None",
          len(files) == 5 and all(f["download_url"] is None and f["downloadable"] is False and f.get("why") == "no_url" for f in files), files[:1])
    check("the project says so too", cf.get_project(900102, fetch=fake)["project"]["distributable"] is False)
    check("listing them did not touch the download-url route (a null link is never worked around)",
          not any("download-url" in c["url"] for c in fake.calls), [c["url"] for c in fake.calls])
    by = {f["file_id"]: f for f in cf.list_files(900100, fetch=fake)["files"]}
    check("a link on an unknown host is not a download link", by["9001000"]["download_url"] is None and by["9001000"]["downloadable"] is False and by["9001000"]["why"] == "host_not_allowed", by["9001000"])
    check("a rejected / malware-flagged file is not downloadable", by["9001006"]["downloadable"] is False and by["9001006"]["why"] == "unavailable", by["9001006"])
    for style in ("failure", "status", "bare"):
        r = cf.download_url(900102, 9102000, fetch=_fake(style))
        check(f"download-url: the 403 is not_distributable ('{style}' shape)", r["ok"] is False and r["reason"] == "not_distributable" and "project page" in r["error"], r)
    fake = _fake()
    r = cf.download_url(900100, 9001004, fetch=fake)
    check("download-url: an allowed file gives its link", r == {"ok": True, "download_url": fake.files[900100][1]["downloadUrl"]}, r)
    fake.files[900104][0]["downloadUrl"] = None
    r = cf.download_url(900104, 9104001, fetch=fake)
    check("download-url: an empty answer is not_distributable too", r["reason"] == "not_distributable", r)
    r = cf.download_url(900100, 9001000, fetch=fake)
    check("download-url: a link on an unknown host is refused", r["ok"] is False and r["reason"] == "vendor_changed", r)
    check("download-url: an unknown file is not_found", cf.download_url(900100, 5, fetch=fake)["reason"] == "not_found")
    check("download-url: bad ids are refused", cf.download_url("a", 1, fetch=fake)["reason"] == "not_found")


# ---- get_file -------------------------------------------------------------------------------
def test_get_file():
    fake = _fake()
    r = cf.get_file("900100", "9001004", fetch=fake)
    check("a file by project and file id", r["ok"] and r["file"]["file_id"] == "9001004" and fake.calls[0]["url"].endswith("/v1/mods/900100/files/9001004"), r)
    check("a file of another project is not_found", cf.get_file(900100, 9101002, fetch=fake)["reason"] == "not_found")
    check("a server pack is unsupported", cf.get_file(900100, 9001001, fetch=fake)["reason"] == "unsupported")
    check("an unknown file is not_found", cf.get_file(900100, 4, fetch=fake)["reason"] == "not_found")
    n = len(fake.calls)
    check("bad ids are refused without a request", cf.get_file("x", 1, fetch=fake)["reason"] == "not_found" and len(fake.calls) == n)


# ---- bulk -----------------------------------------------------------------------------------
def test_bulk():
    fake = _fake()
    refs = [900100, "900101", "test-pack", 999, 900103]
    r = cf.bulk_projects(refs, fetch=fake)
    got = r["projects"]
    check("bulk_projects is keyed by the refs as given; unknown and unhandled-class refs are None",
          r["ok"] and list(got) == refs and got[900100]["id"] == "900100" and got["900101"]["id"] == "900101"
          and got["test-pack"]["id"] == "900106" and got[999] is None and got[900103] is None, {k: (v or {}).get("id") for k, v in got.items()})
    posts = [c for c in fake.calls if c["method"] == "POST"]
    check("the ids go out in ONE bulk call (POST /mods) with filterPcOnly", len(posts) == 1 and posts[0]["url"].endswith("/v1/mods") and posts[0]["json_body"] == {"modIds": [900100, 900101, 999, 900103], "filterPcOnly": True}, posts)
    fake = _fake()
    r = cf.bulk_projects(list(range(1, 121)) + [1, 2], fetch=fake)
    sizes = [len(c["json_body"]["modIds"]) for c in fake.calls]
    check("120 ids are chunked (50, 50, 20), duplicates sent once", r["ok"] and sizes == [50, 50, 20], sizes)
    check("bulk_projects of nothing is an empty answer without a request", cf.bulk_projects([], fetch=fake)["projects"] == {} and len(fake.calls) == 3)
    fake = _fake()
    fake.fail = [403]
    r = cf.bulk_projects([900100], fetch=fake)
    check("a failure that hits every project fails the whole call", r["ok"] is False and r["reason"] == "bad_key", r)

    fake = _fake()
    pairs = [(900100, 9001004), ("900101", "9101002"), (900100, 9101002), (900100, 9001001), (900100, 1), (900100, 9001004)]
    r = cf.bulk_get_files(pairs, fetch=fake)
    out = r["files"]
    check("bulk_get_files is keyed '<project>:<file>', in order, duplicates once",
          r["ok"] and list(out) == ["900100:9001004", "900101:9101002", "900100:9101002", "900100:9001001", "900100:1"], list(out))
    check("it returns the files; a file under another project, a server pack and a missing one are None",
          out["900100:9001004"]["file_id"] == "9001004" and out["900101:9101002"]["file_id"] == "9101002"
          and out["900100:9101002"] is None and out["900100:9001001"] is None and out["900100:1"] is None, out)
    check("one POST /mods/files with the distinct ids", len(fake.calls) == 1 and fake.calls[0]["url"].endswith("/v1/mods/files") and fake.calls[0]["json_body"] == {"fileIds": [9001004, 9101002, 9001001, 1]}, fake.calls)
    fake = _fake()
    cf.bulk_get_files([(900100, 7000000 + i) for i in range(120)], fetch=fake)
    check("file ids are chunked at 50", [len(c["json_body"]["fileIds"]) for c in fake.calls] == [50, 50, 20])
    check("junk pairs are skipped", cf.bulk_get_files([None, (1,), ("a", "b"), "xy"], fetch=_fake())["files"] == {})

    fake = _fake()
    r = cf.bulk_list_files([900100, 900101, 999999], "26.3", "fabric", fetch=fake)
    f = r["files"]
    check("bulk_list_files: the exact list per project (newest first); no match and an unknown project are empty lists",
          r["ok"] and _ids(f[900100]) == ["9001005", "9001004", "9001007", "9001006", "9001000"] and f[900101] == [] and f[999999] == [], {k: _ids(v) for k, v in f.items()})
    fake = _fake()
    fake.fail = [429]
    r = cf.bulk_list_files([900100, 900101], fetch=fake)
    check("a rate limit stops the whole call (the caller gets rate_limited, not a partial answer)", r["ok"] is False and r["reason"] == "rate_limited", r)


# ---- errors and the key ---------------------------------------------------------------------
def test_errors():
    for style in ("failure", "status", "bare"):
        for status, reason in ((403, "bad_key"), (401, "bad_key"), (429, "rate_limited"), (500, "offline"), (503, "offline")):
            fake = _fake(style)
            fake.fail = [status]
            r = cf.search("x", fetch=fake)
            check(f"HTTP {status} ('{style}' shape) is {reason}", r["ok"] is False and r["reason"] == reason and r["reason"] in CONTRACT_REASONS and isinstance(r["error"], str) and r["error"], r)
    fake = _fake()
    fake.fail = [429]
    check("the vendor's wait time is passed on when net gives it", cf.search("x", fetch=fake).get("retry_after") == 7)

    def boom(*a, **k):
        raise RuntimeError("socket exploded " + FAKE_KEY)
    r = cf.search("x", fetch=boom)
    check("a fetch that raises becomes offline, and its text (which held the key) is not repeated",
          r["ok"] is False and r["reason"] == "offline" and FAKE_KEY not in _dump(r), r)
    for label, odd in (("None", lambda *a, **k: None), ("a list", lambda *a, **k: []),
                       ("a body with no data", lambda *a, **k: {"ok": True, "status": 200, "body": {"x": 1}}),
                       ("a body that is text", lambda *a, **k: {"ok": True, "status": 200, "body": "<html>"})):
        _key()
        for name, call in (("search", lambda o=odd: cf.search("x", fetch=o)), ("get_project", lambda o=odd: cf.get_project(900100, fetch=o)),
                           ("list_files", lambda o=odd: cf.list_files(900100, fetch=o)), ("get_file", lambda o=odd: cf.get_file(900100, 1, fetch=o)),
                           ("bulk_projects", lambda o=odd: cf.bulk_projects([900100], fetch=o)), ("bulk_get_files", lambda o=odd: cf.bulk_get_files([(1, 1)], fetch=o))):
            r = call()
            check(f"{name}: an answer that is {label} is vendor_changed", r["ok"] is False and r["reason"] == "vendor_changed", r)
    fake = _fake()
    fake.leak = FAKE_KEY
    fake.fail = [500]
    r = cf.search("x", fetch=fake)
    check("a key a careless net left in an error text is scrubbed", FAKE_KEY not in _dump(r) and "[key]" in r["error"], r)


def test_key_never_appears():
    fake = _fake()
    results = []
    for _name, call in _every_function(fake).items():
        results.append(call())
    results.append(cf.get_project("twin", fetch=fake))
    results.append(cf.search("", kind="shader", fetch=fake))
    fake.fail = [403]
    results.append(cf.search("x", fetch=fake))
    check("with a fake key set, no result, error or recorded call contains it", FAKE_KEY not in _dump(results, fake.calls), "")
    check("no request carries headers or a key parameter",
          all(c["headers"] is None and "key" not in json.dumps(c["params"]).lower() and "api_key" not in json.dumps(c["json_body"]) for c in fake.calls))
    check("every request goes to the CurseForge API host as vendor curseforge",
          all(c["vendor"] == "curseforge" and c["url"].startswith("https://api.curseforge.com/v1/") for c in fake.calls))
    check("every failure uses a contract reason code", all(r.get("ok") or r.get("reason") in CONTRACT_REASONS for r in results), [r for r in results if not r.get("ok")][:2])


def test_ping():
    fake = _fake()
    r = cf.ping(fetch=fake)
    check("ping asks the class list the probe ran (classesOnly as text 'true')", r == {"ok": True} and fake.calls[0]["url"].endswith("/v1/categories") and fake.calls[0]["params"] == {"gameId": 432, "classesOnly": "true"}, fake.calls)
    fake.fail = [403]
    check("ping reports a refused key as bad_key", cf.ping(fetch=fake)["reason"] == "bad_key")


def test_never_raises():
    fake = _fake()
    weird = [None, [], {}, object(), "", 0, -1, 3.5, "x" * 500, float("nan"), b"bytes"]
    bad = []
    for w in weird:
        for name, call in (("search", lambda w=w: cf.search(w, w, w, w, w, w, w, fetch=fake)),
                           ("get_project", lambda w=w: cf.get_project(w, fetch=fake)),
                           ("list_files", lambda w=w: cf.list_files(w, w, w, fetch=fake)),
                           ("get_file", lambda w=w: cf.get_file(w, w, fetch=fake)),
                           ("bulk_projects", lambda w=w: cf.bulk_projects(w, fetch=fake)),
                           ("bulk_list_files", lambda w=w: cf.bulk_list_files(w, w, w, fetch=fake)),
                           ("bulk_get_files", lambda w=w: cf.bulk_get_files(w, fetch=fake)),
                           ("download_url", lambda w=w: cf.download_url(w, w, fetch=fake))):
            try:
                r = call()
            except Exception as exc:  # noqa: BLE001
                bad.append((name, repr(w)[:20], repr(exc)))
                continue
            if not (isinstance(r, dict) and "ok" in r and (r["ok"] or r.get("reason") in CONTRACT_REASONS)):
                bad.append((name, repr(w)[:20], r))
    check("no function raises on odd arguments, and every failure carries a contract reason", not bad, bad[:3])


def test_default_fetch_is_net():
    try:
        spec = importlib.util.find_spec("jarvis.modhosts.net")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        print("skip    net.py exists in this tree; fetch=None would use it and reach the network (not tested here)")
        return
    _key()
    r = cf.search("x")
    check("with no net.py and no fetch, the answer is a plain unsupported (nothing raised)", r["ok"] is False and r["reason"] == "unsupported", r)


# ---- static ---------------------------------------------------------------------------------
def test_static():
    src = (Path(cf.__file__)).read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    for banned in ("urllib", "import requests", "http.client", "import socket", "x-api-key", "curseforge.json", "CURSEFORGE_API_KEY", "mediafilecdn"):
        check(f"curseforge.py never mentions {banned!r} outside comments and its docstring",
              banned not in re.sub(r'"""(.|\n)*?"""', "", code, count=1), banned)
    for name, params in (("search", ["query", "kind", "mc_version", "loader", "sort", "limit", "offset"]), ("get_project", ["ref"]),
                         ("list_files", ["project_id", "mc_version", "loader"]), ("get_file", ["project_id", "file_id"]),
                         ("bulk_projects", ["refs"]), ("bulk_list_files", ["project_ids", "mc_version", "loader"]),
                         ("bulk_get_files", ["pairs"])):
        sig = inspect.signature(getattr(cf, name))
        ps = list(sig.parameters.values())
        check(f"{name}{tuple(params)} matches the contract plus a trailing keyword-only fetch=None",
              [p.name for p in ps[:-1]] == params and ps[-1].name == "fetch" and ps[-1].kind is inspect.Parameter.KEYWORD_ONLY and ps[-1].default is None, str(sig))
    check("kind defaults mod, sort defaults relevance, limit 5, offset 0",
          inspect.signature(cf.search).parameters["kind"].default == "mod" and inspect.signature(cf.search).parameters["sort"].default == "relevance"
          and inspect.signature(cf.search).parameters["limit"].default == 5 and inspect.signature(cf.search).parameters["offset"].default == 0)
    literals = set(re.findall(r'_failure\(\s*"([a-z_]+)"', src))
    check("every reason literal in the adapter is a contract reason code", literals <= CONTRACT_REASONS and literals, literals - CONTRACT_REASONS)
    check("the adapter's own reason table is exactly the contract's", cf._REASONS == CONTRACT_REASONS, cf._REASONS ^ CONTRACT_REASONS)
    check("class ids and loader numbers are the probe's / swagger's",
          cf.CLASS_IDS == {"mod": 6, "modpack": 4471, "resourcepack": 12, "shader": 6552, "datapack": 6945}
          and cf.LOADER_TYPES == {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6} and cf.GAME_ID == 432)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n== {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{name} ran without raising", False, e)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
