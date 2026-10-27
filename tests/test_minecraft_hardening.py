"""Minecraft hardening (agent M3): the CurseForge key and the two hosts of CurseForge.

Pins:
  H1  cache.put refuses the key in every spelling the cache file can carry: as it is, JSON-escaped (a key with a
      backslash or a double quote), JSON-escaped twice (a JSON text held in a string), as a dict key, as part of a
      cache key name, and at any key length (the old guard ignored keys under 8 characters).  Without a key it
      stores normally.
  H2  CurseForge answers are never cached: curseforge.py does not touch cache.py, only the four known files do,
      and after the adapter has run with a key the cache holds nothing.
  H3  A 403 from the CurseForge download host is `cdn_refused` (net.py's own code), a 403 from the API host stays
      `bad_key`, and curseforge.download_url still answers `not_distributable` for both, with a fake transport.
  H4  The consumers of those reasons keep working over the real net.fetch with a fake transport:
      curseforge.fingerprint_matches (crossmatch's route), crossmatch's jar download loop, updates.match_hashes.
  H5  policy.py scores READS of the key file like writes (read_file, a shell `cat`), exactly like ai_config.json,
      and a job's approval does not cover them.
  H6  tests/modhosts_cdn_check.py (the owner-run probe) over a fake transport: the report names the CDN host, the
      403 sample and the fingerprint route's shape; --redact hides names, ids, paths and hash values; the key text
      is in no line, the temporary folder is gone, the key header went to api.curseforge.com only; --list and a
      missing key send nothing.

No network, no real key (every key here is obviously fake), HOME redirected before any jarvis import.
Run: python tests/test_minecraft_hardening.py
"""
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

HOME = tempfile.mkdtemp(prefix="jarvis-minecraft-hardening-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "CURSEFORGE_API_KEY"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "jarvis-cli" / "jarvis"
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import policy  # noqa: E402
from jarvis.modhosts import cache, config, crossmatch, curseforge, net, updates  # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-M3-5d2c9e61a07b4f38"
TRICKY_KEY = 'K3y"with\\back\\slash"q9'                       # K3y"with\back\slash"q9: a double quote and backslashes
CDN = "https://edge.forgecdn.net/files/1/2/a.jar"
KEY_FILE = Path(HOME) / ".jarvis" / "curseforge.json"
CF_API = "https://api.curseforge.com/v1"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


def set_key(key):
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps({"api_key": key}), encoding="utf-8")


def clear_key():
    if KEY_FILE.exists():
        KEY_FILE.unlink()


def ok_json(obj, status=200):
    return net.Reply(status, {"content-type": "application/json"}, json.dumps(obj))


class Net:
    """net's test seams for one block: a transport (a list of replies, the last one repeats, or a function)."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def transport(self, method, url, headers, json_body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "json": json_body})
        if callable(self.replies):
            return self.replies(method, url, headers, json_body, timeout)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def __enter__(self):
        self.saved = (net.TRANSPORT, net.RESOLVER, net.SLEEP, net.CONTACT_PROVIDER)
        net.TRANSPORT = self.transport
        net.RESOLVER = lambda host, port: ["93.184.216.34"]
        net.SLEEP = lambda seconds: None
        net.CONTACT_PROVIDER = lambda: "github.com/GoatAssim"
        return self

    def __exit__(self, *exc):
        net.TRANSPORT, net.RESOLVER, net.SLEEP, net.CONTACT_PROVIDER = self.saved


# ---- H1 --------------------------------------------------------------------------------------------------------
def test_h1_cache_refuses_every_spelling_of_the_key():
    for label, key in (("a plain key", FAKE_KEY), ("a key with a double quote and backslashes", TRICKY_KEY),
                       ("a key shorter than 8 characters", "ab#9"), ('a short key with a double quote', 'x"y')):
        set_key(key)
        check("%s: config hands it out as written" % label, config.get_key() == key, config.get_key())
        cache.clear()
        escaped = json.dumps(key)[1:-1]
        check("%s: a value holding it is refused" % label, cache.put("raw", {"x": "a " + key}, 60) is False)
        check("%s: the JSON-escaped spelling is refused" % label, cache.put("esc", {"x": "a " + escaped}, 60) is False)
        check("%s: a JSON text held in a string (escaped twice) is refused" % label,
              cache.put("nested", json.dumps({"echo": key}), 60) is False)
        check("%s: as a dict key or a list element it is refused" % label,
              cache.put("dk", {key: 1}, 60) is False and cache.put("li", ["a", key], 60) is False)
        check("%s: a cache key NAME holding it is refused" % label, cache.put("name-" + key, 1, 60) is False)
        check("%s: an ordinary value is still stored" % label, cache.put("fine", {"x": 1}, 60) is True and cache.get("fine") == {"x": 1})
        text = (Path(HOME) / ".jarvis" / "modhosts" / "cache.json").read_text(encoding="utf-8")
        check("%s: the cache file holds it in no spelling" % label, key not in text and escaped not in text, text[:200])
    clear_key()
    cache.clear()
    check("with no key set nothing is refused for that reason", cache.put("any", {"x": "ab#9"}, 60) is True)


# ---- H2 --------------------------------------------------------------------------------------------------------
def _uses_cache(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(a.name == "cache" for a in node.names) or (node.module or "").split(".")[-1] == "cache":
                return True
        if isinstance(node, ast.Import) and any(a.name.endswith("modhosts.cache") for a in node.names):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith("modhosts.cache"):
            return True
    return False


def test_h2_curseforge_answers_are_never_cached():
    check("curseforge.py does not touch cache.py", not _uses_cache(PKG / "modhosts" / "curseforge.py"))
    users = sorted([p.name for p in (PKG / "modhosts").glob("*.py") if p.name != "cache.py" and _uses_cache(p)]
                   + ["actions/" + p.name for p in (PKG / "actions").glob("minecraft_*.py") if _uses_cache(p)])
    check("only the four known files use the cache (a new one is a decision: say why in AGENTS.md and add it here)",
          users == ["crossmatch.py", "loaders.py", "modrinth.py", "optifine.py"], users)
    set_key(FAKE_KEY)
    cache.clear()

    def handler(method, url, headers, json_body, timeout):
        if "/fingerprints/" in url:
            return ok_json({"data": {"exactMatches": [], "exactFingerprints": [], "unmatchedFingerprints": [1]}})
        return ok_json({"data": [], "pagination": {"index": 0, "pageSize": 50, "resultCount": 0, "totalCount": 0}})

    with Net(handler) as world:
        results = [curseforge.search("x"), curseforge.get_project(5), curseforge.list_files(5), curseforge.download_url(5, 6),
                   curseforge.fingerprint_matches([1, 2]), curseforge.ping()]
    check("the adapter ran (requests were made, every answer is a dict)", len(world.calls) >= 6 and all(isinstance(r, dict) for r in results),
          [c["url"] for c in world.calls])
    check("...and the cache holds nothing afterwards", cache._load() == {}, cache._load())
    check("...and the key text is not in the cache file",
          FAKE_KEY not in (Path(HOME) / ".jarvis" / "modhosts" / "cache.json").read_text(encoding="utf-8"))
    clear_key()


# ---- H3 --------------------------------------------------------------------------------------------------------
def test_h3_download_url_still_says_not_distributable():
    set_key(FAKE_KEY)
    with Net([net.Reply(403, {}, b"")]) as world:
        res = curseforge.download_url(900100, 9001004)
    check("the API's 403 for download-url is not_distributable, with the project page in the sentence",
          res.get("ok") is False and res.get("reason") == "not_distributable" and "project page" in res.get("error", ""), res)
    check("...and the key went to the API host only", all(c["headers"].get("x-api-key") == FAKE_KEY for c in world.calls)
          and all(urlsplit(c["url"]).hostname == "api.curseforge.com" for c in world.calls), world.calls)

    route = "%s/mods/900100/files/9001004/download-url" % CF_API
    with Net([net.Reply(302, {"Location": CDN}), net.Reply(403, {}, b"")]) as world:
        raw = net.fetch("curseforge", route)
    check("net.fetch: an API route that redirects to the CDN, which refuses, is cdn_refused", raw.get("reason") == "cdn_refused"
          and raw.get("status") == 403, raw)
    with Net([net.Reply(302, {"Location": CDN}), net.Reply(403, {}, b"")]) as world:
        res = curseforge.download_url(900100, 9001004)
    check("curseforge.download_url folds that into not_distributable too (exactly as before the change)",
          res.get("ok") is False and res.get("reason") == "not_distributable", res)
    check("...and the key header went to the first request only",
          "x-api-key" in world.calls[0]["headers"] and "x-api-key" not in world.calls[1]["headers"], world.calls)

    def fake(vendor, url, **kw):
        return {"ok": False, "reason": "cdn_refused", "error": "the download server refused it", "status": 403}

    res = curseforge.download_url(900100, 9001004, fetch=fake)
    check("a fetch that says cdn_refused with its 403 status is not_distributable as well", res.get("reason") == "not_distributable", res)

    with Net([ok_json({"data": "https://edge.forgecdn.net/files/9/9/a.jar"})]):
        res = curseforge.download_url(900100, 9001004)
    check("an allowed file still gives its link", res == {"ok": True, "download_url": "https://edge.forgecdn.net/files/9/9/a.jar"}, res)
    clear_key()


# ---- H4 --------------------------------------------------------------------------------------------------------
class Ctx:
    """The two methods crossmatch._fingerprint_downloads asks of its context."""

    def __init__(self):
        self.skips = []

    def tick(self):
        pass

    def skip(self, ref, reason, text, **kw):
        self.skips.append((ref, reason, kw.get("stage")))


def _jar_item():
    return {"file": {"download_url": "https://cdn.modrinth.com/data/a/versions/b/a.jar", "size": 3,
                     "hashes": {"sha1": "a" * 40, "sha512": "b" * 128}},
            "project": {"name": "A"}, "entry": {"ref": "ref-a"}}


def test_h4_consumers_of_the_reasons_keep_working():
    set_key(FAKE_KEY)
    raw_file = {"id": 22, "modId": 11, "fileName": "a.jar", "fileFingerprint": 12345, "releaseType": 1, "fileLength": 3,
                "downloadUrl": "https://edge.forgecdn.net/files/1/2/a.jar", "gameVersions": ["1.20.1", "Fabric"],
                "hashes": [{"algo": 1, "value": "a" * 40}], "isAvailable": True}
    with Net([ok_json({"data": {"exactMatches": [{"id": 11, "file": raw_file}], "exactFingerprints": [12345]}})]) as world:
        got = curseforge.fingerprint_matches([12345])
    check("fingerprint_matches over net.fetch: a match is read", got.get("ok") is True and got["matches"][12345][0]["file_id"] == "22", got)
    check("...POSTed to the fingerprint route on the API host, key header set",
          world.calls[0]["method"] == "POST" and urlsplit(world.calls[0]["url"]).path == "/v1/fingerprints/432"
          and world.calls[0]["headers"].get("x-api-key") == FAKE_KEY, world.calls[0])
    with Net([net.Reply(403, {}, b"")]):
        got = curseforge.fingerprint_matches([12345])
    check("an API-host 403 on the fingerprint route is still bad_key (and says which route)",
          got.get("ok") is False and got.get("reason") == "bad_key" and "fingerprint" in got.get("error", ""), got)

    check("crossmatch still treats bad_key as fatal and cdn_refused as no reason to stop a whole pack",
          "bad_key" in crossmatch._FATAL and net.CDN_REFUSED not in crossmatch._FATAL, crossmatch._FATAL)
    saved_fetch = crossmatch.FETCH
    try:
        crossmatch.FETCH = None
        for status, expect_stop, reason in ((403, False, "vendor_changed"), (401, True, "bad_key"), (500, True, "offline")):
            ctx = Ctx()
            with Net([net.Reply(status, {}, b"")]):
                stop = crossmatch._fingerprint_downloads(ctx, [_jar_item()], {}, {"downloaded": 0, "bytes": 0}, 1000)
            if expect_stop:
                check("crossmatch: a Modrinth jar answered HTTP %d stops the pack with %s" % (status, reason),
                      isinstance(stop, dict) and stop.get("reason") == reason and not ctx.skips, (stop, ctx.skips))
            else:
                check("crossmatch: a Modrinth jar answered HTTP %d is skipped as %s, the run goes on" % (status, reason),
                      stop is None and ctx.skips == [("ref-a", reason, "match")], (stop, ctx.skips))
        ctx = Ctx()
        crossmatch.FETCH = lambda vendor, url, **kw: {"ok": False, "reason": "cdn_refused", "error": "refused", "status": 403}
        stop = crossmatch._fingerprint_downloads(ctx, [_jar_item()], {}, {"downloaded": 0, "bytes": 0}, 1000)
        check("crossmatch: even a cdn_refused answer is one skipped file, not a stop",
              stop is None and ctx.skips == [("ref-a", "vendor_changed", "match")], (stop, ctx.skips))
    finally:
        crossmatch.FETCH = saved_fetch

    for status, reason in ((403, "vendor_changed"), (401, "bad_key"), (503, "offline")):
        with Net([net.Reply(status, {}, b"")]):
            matched, failure, errors = updates.match_hashes(["a" * 40])
        check("updates.match_hashes: Modrinth HTTP %d is %s, nothing matched" % (status, reason),
              matched == {} and failure is not None and failure.get("reason") == reason and "a" * 40 in errors, (matched, failure))
    with Net([ok_json({})]):
        matched, failure, errors = updates.match_hashes(["a" * 40])
    check("updates.match_hashes: an empty answer is no failure", matched == {} and failure is None and errors == {}, (matched, failure, errors))
    clear_key()


# ---- H5 --------------------------------------------------------------------------------------------------------
def test_h5_policy_scores_reads_of_the_key_file():
    def decide(tool, args):
        return policy.decide(tool, args, policy=policy.DEFAULT_POLICY)

    def names_the_key(res):
        return any("your CurseForge API key" in str(r) for r in res["reasons"])

    mine = decide("read_file", {"path": "~/.jarvis/curseforge.json"})
    theirs = decide("read_file", {"path": "~/.jarvis/ai_config.json"})
    check("read_file of the key file is not allowed silently: it is confirm, and the reason names the key",
          mine["decision"] == "confirm" and names_the_key(mine), mine)
    check("...the same decision as for ai_config.json", mine["decision"] == theirs["decision"] and mine["score"] == theirs["score"], (mine["score"], theirs["score"]))
    check("an ordinary file is allowed (the guard is not crying wolf)", decide("read_file", {"path": "~/Documents/notes.txt"})["decision"] == "allow")
    shell = decide("run_custom_command", {"command": "cat ~/.jarvis/curseforge.json"})
    check("a shell `cat` of the key file is not allowed silently either", shell["decision"] != "allow" and names_the_key(shell), shell)
    check("a job's approval does not cover it (the sensitive-path floor)", bool(policy._approval_floor(mine["reasons"])), mine["reasons"])


# ---- H6 --------------------------------------------------------------------------------------------------------
PROBE_KEY = "$2a$10$FAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFA"       # key-shaped, obviously fake
JAR = b"PK\x03\x04" + b"tiny mod " * 40
SHA1 = hashlib.sha1(JAR).hexdigest()
FP = crossmatch.cf_fingerprint(JAR)
PAGES = {"index": 0, "pageSize": 50, "resultCount": 1, "totalCount": 1}
FILE = {"id": 9000011, "modId": 900001, "displayName": "Tiny 1.0", "fileName": "tiny-1.0.jar", "fileDate": "2026-01-01T00:00:00Z",
        "fileLength": len(JAR), "downloadUrl": "https://edge.forgecdn.net/files/9000/11/tiny-1.0.jar", "releaseType": 1,
        "gameVersions": ["1.20.1", "Fabric"], "hashes": [{"algo": 1, "value": SHA1}], "fileFingerprint": FP,
        "isAvailable": True, "isServerPack": False, "dependencies": []}
MOD = {"id": 900001, "classId": 6, "slug": "tiny-mod", "name": "Tiny Mod", "summary": "small", "allowModDistribution": True,
       "downloadCount": 5, "links": {}, "authors": [], "categories": []}
BLOCKED_FILE = dict(FILE, id=448001, modId=448233, downloadUrl=None)


def probe_world(requests):
    def handler(method, url, headers, json_body, timeout):
        requests.append(url)
        parts = urlsplit(url)
        path = parts.path
        if parts.hostname == "edge.forgecdn.net":
            if path == "/":
                return net.Reply(403, {"content-type": "text/html"}, b"<html>denied</html>")
            return net.Reply(200, {"content-type": "application/java-archive", "content-length": str(len(JAR))}, JAR)
        if path == "/v1/mods/search":
            return ok_json({"data": [MOD], "pagination": PAGES})
        if path == "/v1/mods/900001/files":
            return ok_json({"data": [FILE], "pagination": PAGES})
        if path == "/v1/mods/448233/files":
            return ok_json({"data": [BLOCKED_FILE], "pagination": PAGES})
        if path.endswith("/download-url"):
            return net.Reply(403, {}, b"")
        if path == "/v1/fingerprints/432":
            asked = json_body["fingerprints"]
            return ok_json({"data": {"exactMatches": [{"id": 900001, "file": FILE}] if FP in asked else [],
                                     "exactFingerprints": [f for f in asked if f == FP],
                                     "unmatchedFingerprints": [f for f in asked if f != FP]}})
        return net.Reply(404, {}, b"")
    return handler


def _load_probe():
    spec = importlib.util.spec_from_file_location("modhosts_cdn_check", ROOT / "tests" / "modhosts_cdn_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_h6_the_probe_over_a_fake_transport():
    probe = _load_probe()
    src = (ROOT / "tests" / "modhosts_cdn_check.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    for banned in ("import urllib.request", "urlopen", "import requests", "http.client", "get_key(", "CURSEFORGE_API_KEY", "curseforge.json"):
        check("the probe never mentions %r (it goes through net.fetch and config.public_view)" % banned, banned not in code)
    check("it downloads through net.fetch", 'net.fetch("curseforge", url, want="bytes"' in src)

    scrubber = probe.Report(False, lambda s: None)
    check("Report.scrub removes anything shaped like a CurseForge key and notes it",
          PROBE_KEY not in scrubber.scrub("sent " + PROBE_KEY + " back") and scrubber.warnings, scrubber.warnings)

    set_key(PROBE_KEY)
    tmp_root = Path(HOME) / "tmproot"
    tmp_root.mkdir(exist_ok=True)
    saved_tmp = tempfile.tempdir
    tempfile.tempdir = str(tmp_root)
    try:
        requests, lines = [], []
        with Net(probe_world(requests)) as world:
            code_, text = probe.run([], out=lines.append)
        everything = text + "\n".join(lines)
        check("the run ends with exit code 0 (everything checked out)", code_ == 0, text[-900:])
        check("the report names the download host", "edge.forgecdn.net" in text and "host used for the download: edge.forgecdn.net" in text, text[-600:])
        check("it says the file's size, SHA-1 and fingerprint matched the API", all(
            ("[ok]   %s" % name) in text for name in ("size", "sha1", "cf_fingerprint equals the API's fileFingerprint")), text)
        check("it records the fingerprint route's answer and its shape", "fingerprint route answers" in text
              and "exactMatches: list[1]" in text, text)
        check("it records a CDN 403 sample as cdn_refused", "SEEN on the host's root address" in text and "reason=cdn_refused" in text, text)
        check("it records the API's 403 for a blocked file and that download_url folds it into not_distributable",
              "reason=bad_key" in text and "download_url folds it into not_distributable" in text, text)
        check("the key text is in no report line and no console line", PROBE_KEY not in everything and not probe.KEY_SHAPE.search(everything))
        check("the request log lists header names, and the key header only on api.curseforge.com lines",
              all("api.curseforge.com" in line for line in lines if "[KEY HEADER]" in line) and any("[KEY HEADER]" in line for line in lines))
        check("the key header went only to the API host on the wire too",
              all(urlsplit(c["url"]).hostname == "api.curseforge.com" for c in world.calls if "x-api-key" in c["headers"]), world.calls[:2])
        check("the temporary folder is gone", list(tmp_root.iterdir()) == [], list(tmp_root.iterdir()))
        check("the console got the same lines as the report", "\n".join(lines).strip() == text.strip())

        requests2, lines2 = [], []
        with Net(probe_world(requests2)):
            code2, text2 = probe.run(["--redact"], out=lines2.append)
        check("--redact: still exit 0 and the hosts, statuses and yes/no lines stay",
              code2 == 0 and "edge.forgecdn.net" in text2 and "[ok]   sha1" in text2 and "cdn_refused" in text2, text2[-700:])
        hidden = ["Tiny Mod", "tiny-mod", "tiny-1.0.jar", "9000011", "900001", SHA1, str(FP), PROBE_KEY, HOME, "/v1/mods"]
        leaked = [h for h in hidden if h in text2 or h in "\n".join(lines2)]
        check("--redact hides the mod's name, slug, ids, file name, hashes, fingerprint, URL paths and local folders", not leaked, leaked)

        before = len(requests2)
        lines3 = []
        with Net(probe_world(requests2)):
            code3, text3 = probe.run(["--list"], out=lines3.append)
        check("--list sends nothing and names the planned hosts", code3 == 0 and len(requests2) == before
              and "edge.forgecdn.net" in text3 and "fingerprints/432" in text3, text3)

        clear_key()
        requests4 = []
        with Net(probe_world(requests4)):
            code4, text4 = probe.run([], out=lambda s: None)
        check("with no key it asks nothing and exits 2", code4 == 2 and requests4 == [] and "No usable CurseForge key" in text4, text4)
    finally:
        tempfile.tempdir = saved_tmp
        clear_key()


# --- runner (keep BELOW every test: it reads globals() when it executes) ----------------------------------------

if __name__ == "__main__":
    for name in sorted(n for n in list(globals()) if n.startswith("test_")):
        print(f"\n== {name}")
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((name, f"raised {exc!r}"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for item in FAIL:
        print("  FAILED:", item)
    sys.exit(1 if FAIL else 0)
