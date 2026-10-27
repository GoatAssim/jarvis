"""check_mc_updates, the CurseForge half and the safe default folder (agent M1), against an in-process fake Modrinth AND CurseForge.

Covers:
  * no key: the CurseForge half is skipped with ONE hint (curseforge.why no_key), never an error; no CurseForge request is made
    and no file is read for a fingerprint; the files stay `not_on_modrinth`;
  * with a key: crossmatch's sanity question (a fingerprint nobody has) is asked FIRST, then one lookup of the real fingerprints;
    only numbers leave the PC (no name, path, sha1 or key in any CurseForge body), the key header goes to api.curseforge.com only;
  * a match needs the fingerprint AND the SHA-1: a fingerprint with another SHA-1 is not used, two different identical files are
    `ambiguous`, nothing else is a match (`not_on_curseforge`); a look-alike name is never evidence;
  * "newer" is by publication date for the same Minecraft version and loader, with the Modrinth path's channel rules; Quilt falls back
    to Fabric (`via: fabric`) and the setting switches it off; several versions / loaders without an answer is a question; a file
    built for another version or loader is `other_version`; resource packs have no loader; unavailable files are never offered;
  * a project that blocks third-party downloads is `not_distributable`, listed with its page, and no download route is ever asked;
  * the UNVERIFIED route: an odd or changed answer (a match for the fingerprint nobody has, a wrong shape, a refused route) marks the
    whole CurseForge half `vendor_changed` and the Modrinth half still reports; a refused key / rate limit / failing listing is
    contained the same way; the note and the message both say "UNVERIFIED";
  * each update carries get_with (download_mc_file, source curseforge, project, file id);
  * the fingerprint is cached next to the SHA-1 in update_state.json (same size + mtime rule): nothing is read twice, a changed file is
    re-read, old three-value entries still load, a damaged fingerprint is dropped; no key and no vendor answer is in the file;
  * no folder named and none checked before: `needs_folder` with an ask line naming default_dest; nothing is read, hashed, sent or
    remembered; a named folder is then checked and becomes the last one;
  * hygiene: updates.py never passes "curseforge" to net.fetch, never names the key file or calls get_key(), opens no connection.

Fixture: tests/data/modhosts/hand_m1_curseforge.json (git-ignored folder: the owner adds it with `git add -f`).
No network.  Run: python tests/test_minecraft_updates_cf.py
"""
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = tempfile.mkdtemp(prefix="jarvis-minecraft-updates-cf-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER",
             "JARVIS_TOOL_MAKER_CONTEXT_FILE", "CURSEFORGE_API_KEY"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import tool_result_shaping  # noqa: E402
from jarvis.actions import minecraft_updates as mu  # noqa: E402
from jarvis.modhosts import config, crossmatch, curseforge, net, untrusted, updates  # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-M1-not-a-real-key-0123456789"
GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
HOMEP = Path(HOME)
FIXTURE = json.loads((ROOT / "tests" / "data" / "modhosts" / "hand_m1_curseforge.json").read_text(encoding="utf-8"))
FAB = {"mc_version": "1.20.1", "loader": "fabric"}
_n = [0]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


def sha1(seed):
    return hashlib.sha1(seed.encode()).hexdigest()


def sha512(seed):
    return hashlib.sha512(seed.encode()).hexdigest()


def fp(seed):
    return crossmatch.cf_fingerprint(seed.encode())


# ------------------------------------------------------------------ the fake vendors

class Fake:
    """A net.TRANSPORT answering Modrinth (one project, two versions) and CurseForge (the fixture).  `calls` records every
    request.  Knobs: `cf_fail` {path fragment: status}, `odd_probe` (the fingerprint nobody has is "found"), `bad_shape` (the
    fingerprint answer has no exactMatches), `extra_matches` {fingerprint: [file raw dict]} (added to what the real data gives),
    `fp_lies` {fingerprint: [(mod id, file raw dict)]} (answered for that number whatever the data says)."""

    def __init__(self):
        self.calls = []
        self.cf_fail, self.odd_probe, self.bad_shape, self.fp_lies = {}, False, False, {}
        self.mods = {m["id"]: m for m in FIXTURE["mods"]}
        self.raw = {}                                         # file id -> raw vendor file dict
        self.by_mod = {}
        for mod_id, items in FIXTURE["files"].items():
            for item in items:
                raw = self._raw(int(mod_id), item)
                self.raw[raw["id"]] = raw
                self.by_mod.setdefault(int(mod_id), []).append(raw)
        # Modrinth: one project, an old and a new version of the same Fabric 1.20.1 mod
        self.mr_versions = [self._mr_version("VerSod050", "0.5.0", "2026-01-01T00:00:00.000Z"),
                            self._mr_version("VerSod051", "0.5.1", "2026-03-01T00:00:00.000Z")]
        self.mr_by_hash = {v["files"][0]["hashes"]["sha1"]: v for v in self.mr_versions}

    @staticmethod
    def _raw(mod_id, item):
        seed, fp_seed = item["seed"], item.get("seed_fp") or item["seed"]
        url = ("https://edge.forgecdn.net/files/%d/%d/%s" % (item["id"] // 1000, item["id"] % 1000, item["name"])) if item["url"] else None
        return {"id": item["id"], "modId": mod_id, "displayName": item["name"][:-4].replace("-", " "), "fileName": item["name"],
                "fileDate": item["date"], "fileLength": len(seed.encode()), "downloadUrl": url, "releaseType": item["type"],
                "gameVersions": list(item["versions"]),
                "hashes": [{"algo": 1, "value": sha1(seed)}, {"algo": 2, "value": hashlib.md5(seed.encode()).hexdigest()}],
                "fileFingerprint": fp(fp_seed), "dependencies": [], "isServerPack": False, "isAvailable": item["available"],
                "fileStatus": 4 if item["available"] else 7}

    @staticmethod
    def _mr_version(vid, number, date):
        seed = "sodiumish-" + number
        return {"id": vid, "project_id": "Pj1Sodium", "name": "Sodiumish " + number, "version_number": number,
                "version_type": "release", "date_published": date, "game_versions": ["1.20.1"], "loaders": ["fabric"],
                "files": [{"hashes": {"sha1": sha1(seed), "sha512": sha512(seed)}, "filename": "sodiumish-%s.jar" % number,
                           "size": len(seed), "primary": True, "url": "https://cdn.modrinth.com/data/x/%s.jar" % number}]}

    # -- plumbing
    def __enter__(self):
        self.saved = (net.TRANSPORT, net.RESOLVER, net.CONTACT_PROVIDER, net.SLEEP)
        net.TRANSPORT = self.transport
        net.RESOLVER = lambda host, port: ["93.184.216.34"]
        net.CONTACT_PROVIDER = lambda: "example.invalid"
        net.SLEEP = lambda seconds: None
        return self

    def __exit__(self, *exc):
        net.TRANSPORT, net.RESOLVER, net.CONTACT_PROVIDER, net.SLEEP = self.saved

    @staticmethod
    def _reply(obj, status=200, headers=None):
        h = {"content-type": "application/json"}
        h.update(headers or {})
        return net.Reply(status, h, json.dumps(obj))

    def cf_calls(self):
        return [c for c in self.calls if c["host"] == "api.curseforge.com"]

    def mr_calls(self):
        return [c for c in self.calls if c["host"] == "api.modrinth.com"]

    def fp_calls(self):
        return [c for c in self.cf_calls() if "/fingerprints/" in c["path"]]

    # -- the routes
    def transport(self, method, url, headers, json_body, timeout):
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.calls.append({"method": method, "host": parts.hostname, "path": parts.path, "query": query,
                           "json": copy.deepcopy(json_body), "headers": dict(headers), "url": url})
        if parts.hostname == "api.modrinth.com":
            return self._modrinth(method, parts.path, query, json_body)
        if parts.hostname == "api.curseforge.com":
            for fragment, status in self.cf_fail.items():
                if fragment in parts.path:
                    return self._reply({}, status, {"retry-after": "120"} if status == 429 else None)
            return self._curseforge(method, parts.path, query, json_body)
        return self._reply({}, 404)

    def _modrinth(self, method, path, query, body):
        if method == "POST" and path == "/v2/version_files":
            return self._reply({h: self.mr_by_hash[h] for h in body.get("hashes") or [] if h in self.mr_by_hash})
        if method == "POST" and path == "/v2/version_files/update":
            newest = max(self.mr_versions, key=lambda v: v["date_published"])
            return self._reply({h: newest for h in body.get("hashes") or [] if h in self.mr_by_hash})
        if method == "GET" and path == "/v2/projects":
            return self._reply([{"id": "Pj1Sodium", "slug": "sodiumish", "title": "Sodiumish"}])
        return self._reply({}, 404)

    def _curseforge(self, method, path, query, body):
        if method == "POST" and path == "/v1/fingerprints/432":
            if self.bad_shape:
                return self._reply({"data": {"nothing": "here"}})
            asked = set((body or {}).get("fingerprints") or [])
            exact = []
            if self.odd_probe and 1 in asked:
                exact.append({"id": 1001, "file": dict(self.raw[5001], fileFingerprint=1)})
            for number in asked:
                for mod_id, raw in self.fp_lies.get(number, []):
                    exact.append({"id": mod_id, "file": dict(raw, fileFingerprint=number)})
            for raw in self.raw.values():
                if raw["fileFingerprint"] in asked:
                    exact.append({"id": raw["modId"], "file": raw})
            return self._reply({"data": {"exactMatches": exact, "exactFingerprints": sorted({e["file"]["fileFingerprint"] for e in exact}),
                                         "unmatchedFingerprints": []}})
        if method == "POST" and path == "/v1/mods":
            ids = [int(i) for i in (body or {}).get("modIds", [])]
            return self._reply({"data": [copy.deepcopy(self.mods[i]) for i in ids if i in self.mods]})
        m = re.match(r"^/v1/mods/(\d+)/files$", path)
        if method == "GET" and m:
            items = self.by_mod.get(int(m.group(1)), []) if int(query.get("index", "0")) == 0 else []
            return self._reply({"data": copy.deepcopy(items), "pagination": {"index": 0, "pageSize": 50, "resultCount": len(items),
                                                                              "totalCount": len(items)}})
        return self._reply({}, 404)


class WithKey:
    """A usable (obviously fake) key in the temp HOME for a `with` block; written through config's own path function."""

    def __enter__(self):
        path = config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_key": FAKE_KEY}), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        try:
            config.config_path().unlink()
        except OSError:
            pass


class Patch:
    def __init__(self, obj, **changes):
        self.obj, self.changes = obj, changes

    def __enter__(self):
        self.old = {k: getattr(self.obj, k) for k in self.changes}
        for k, v in self.changes.items():
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.obj, k, v)


def fresh(files=None):
    """A new folder under the temp HOME holding {name: seed}; returns its Path."""
    _n[0] += 1
    folder = HOMEP / ("mods%d" % _n[0])
    folder.mkdir()
    for name, seed in (files or {}).items():
        (folder / name).write_bytes(seed.encode())
    return folder


def state_file():
    return HOMEP / ".jarvis" / "modhosts" / "update_state.json"


def wipe_state():
    try:
        state_file().unlink()
    except OSError:
        pass


def snapshot(folder):
    out = []
    for dirpath, dirnames, filenames in os.walk(str(folder)):
        for name in sorted(dirnames + filenames):
            st = os.lstat(os.path.join(dirpath, name))
            out.append((os.path.relpath(os.path.join(dirpath, name), str(folder)), st.st_size, st.st_mtime_ns))
    return sorted(out)


def run(folder, **kw):
    kw.setdefault("default_folder", str(HOMEP / "Downloads"))
    return updates.check_folder(folder=str(folder) if folder is not None else None, **kw)


def by_file(res, key):
    return {e["file"]: e for e in res.get(key, [])}


def count_fingerprint_reads():
    """Patch crossmatch.fingerprint_file with a spy; returns (patch, list of base names)."""
    seen, real = [], crossmatch.fingerprint_file
    return Patch(crossmatch, fingerprint_file=lambda p: seen.append(os.path.basename(p)) or real(p)), seen


# ------------------------------------------------------------------ no key

def test_no_key_skips_the_half_with_one_hint_and_reads_nothing():
    wipe_state()
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
    spy, read = count_fingerprint_reads()
    with Fake() as fake, spy:
        res = run(folder, **FAB)
    check("it is an answer, never an error", res.get("ok") is True and "error" not in res, res)
    check("the Modrinth half reported", [e["file"] for e in res["updates"]] == ["sodiumish-0.5.0.jar"], res["updates"])
    unc = by_file(res, "not_checked")
    check("the CurseForge jar stays 'not checked: not_on_modrinth'", list(unc) == ["fabricish-1.0.0.jar"]
          and unc["fabricish-1.0.0.jar"]["why"] == "not_on_modrinth", unc)
    check("ONE hint: curseforge.why is no_key, status skipped, unverified", res["curseforge"]["why"] == "no_key"
          and res["curseforge"]["status"] == "skipped" and res["curseforge"]["verified"] is False, res.get("curseforge"))
    check("the note says a key is needed and that nothing was guessed", "key" in res["curseforge_note"] and "guessed" in res["curseforge_note"], res["curseforge_note"])
    check("no request to CurseForge was made", fake.cf_calls() == [], fake.cf_calls())
    check("no file was read for a fingerprint", read == [], read)
    data = json.loads(state_file().read_text(encoding="utf-8"))
    check("the state file holds three values per file (no fingerprint was worked out)", all(len(v) == 3 for v in data["hashes"].values()), data)
    check("the answer is plain JSON and has no key in it", json.loads(json.dumps(res)) == res and FAKE_KEY not in json.dumps(res))


def test_only_modrinth_files_means_curseforge_is_never_asked():
    folder = fresh({"sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
    with Fake() as fake, WithKey():
        res = run(folder, **FAB)
    check("every file was known to Modrinth", res["checked"] == 1 and "curseforge" not in res and "curseforge_note" not in res, res)
    check("so CurseForge got no request at all, not even the sanity one", fake.cf_calls() == [], fake.cf_calls())


# ------------------------------------------------------------------ with a key

def test_sanity_first_then_numbers_only():
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "sodiumish-0.5.0.jar": "sodiumish-0.5.0",
                    "mystery.jar": "bytes nobody has seen"})
    before = snapshot(folder)
    with Fake() as fake, WithKey():
        res = run(folder, **FAB)
    fps = fake.fp_calls()
    check("two fingerprint lookups: the sanity question first, then the real numbers", len(fps) == 2
          and fps[0]["json"] == {"fingerprints": [crossmatch_probe()]}
          and sorted(fps[1]["json"]["fingerprints"]) == sorted([fp("fabricish-cf-1.0.0"), fp("bytes nobody has seen")]), [c["json"] for c in fps])
    check("the sanity question came before every other CurseForge request", fake.cf_calls()[0] is fps[0], [c["path"] for c in fake.cf_calls()])
    sent = json.dumps([c["json"] for c in fake.cf_calls()] + [c["query"] for c in fake.cf_calls()] + [c["path"] for c in fake.cf_calls()])
    check("only numbers (and ids) went to CurseForge: no name, path, sha1 or key", "fabricish-1" not in sent and "mystery" not in sent
          and HOME not in sent and sha1("fabricish-cf-1.0.0") not in sent and FAKE_KEY not in sent, sent[:300])
    check("every fingerprint body is {'fingerprints': [ints]} and nothing else", all(set(c["json"]) == {"fingerprints"}
          and all(isinstance(x, int) for x in c["json"]["fingerprints"]) for c in fps))
    check("the key header went to api.curseforge.com and nowhere else",
          all(c["headers"].get("x-api-key") == FAKE_KEY for c in fake.cf_calls()) and not any("x-api-key" in c["headers"] for c in fake.mr_calls()))
    check("no request to any other host", {c["host"] for c in fake.calls} == {"api.modrinth.com", "api.curseforge.com"}, {c["host"] for c in fake.calls})
    check("nothing was downloaded: no CDN host and no download-url route", not any("forgecdn" in c["url"] or "download-url" in c["path"] for c in fake.calls))
    check("the key is nowhere in the answer or the state file", FAKE_KEY not in json.dumps(res) and FAKE_KEY not in state_file().read_text(encoding="utf-8"))
    ups = by_file(res, "updates")
    check("the CurseForge jar has an update, the Modrinth jar too", sorted(ups) == ["fabricish-1.0.0.jar", "sodiumish-0.5.0.jar"], sorted(ups))
    check("the file nobody knows is 'not checked' with its own reason", by_file(res, "not_checked")["mystery.jar"]["why"] == "not_on_curseforge", res["not_checked"])
    check("the folder was not changed", snapshot(folder) == before)


def crossmatch_probe():
    return getattr(curseforge, "FINGERPRINT_PROBE", 1)


def test_an_update_found_on_curseforge():
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0"})
    with Fake() as fake, WithKey():
        res = run(folder, **FAB)
    e = by_file(res, "updates")["fabricish-1.0.0.jar"]
    check("get_with names the tool, source curseforge, the project and the exact file id",
          e["get_with"] == {"tool": "download_mc_file", "source": "curseforge", "project": "1001", "file": "5002"}, e["get_with"])
    check("the newest RELEASE for this Minecraft version and loader (not the beta, not the other Minecraft version)",
          e["latest"]["file_id"] == "5002" and e["latest"]["channel"] == "release" and e["latest"]["file_name"] == "fabricish-1.1.0.jar", e["latest"])
    check("installed and latest carry their day", e["installed"]["date"] == "2026-01-10" and e["latest"]["date"] == "2026-02-10", e)
    check("project block: id, name, page", e["project"]["id"] == "1001" and e["project"]["name"] == "Fabricish CF"
          and e["project"]["page_url"].startswith("https://www.curseforge.com/"), e["project"])
    check("the entry says where it came from and how it was matched", e["source"] == "curseforge" and e["matched_by"] == "fingerprint", e)
    check("the block says checked, unverified, one number asked and one matched", res["curseforge"]["status"] == "checked"
          and res["curseforge"]["verified"] is False and res["curseforge"]["asked"] == 1 and res["curseforge"]["matched"] == 1, res["curseforge"])
    check("the note says UNVERIFIED", "UNVERIFIED" in res["curseforge_note"], res["curseforge_note"])
    check("so does the message, which also says nothing was changed", "UNVERIFIED" in res["message"] and "Nothing was downloaded" in res["message"]
          and "Modrinth and CurseForge" in res["message"], res["message"])
    check("sources lists both, source is still modrinth, counts include the CurseForge file", res["sources"] == ["modrinth", "curseforge"]
          and res["source"] == "modrinth" and res["checked"] == 1 and res["updates_available"] == 1, res)
    check("every existing result key is still there", {"ok", "source", "folder", "folder_from", "nothing_changed", "ignored", "channel", "checked",
                                                      "updates_available", "updates", "up_to_date", "not_checked", "message", "curseforge_note",
                                                      "untrusted_note"} <= set(res), sorted(res))
    check("the Quilt-less answer has no `via`", "via" not in e["latest"])
    check("a CurseForge id never went to Modrinth: its only requests were the hash lookup (no project-name call)",
          sorted({c["path"] for c in fake.mr_calls()}) == ["/v2/version_files"], [c["path"] for c in fake.mr_calls()])


def test_channels_dates_and_versions():
    folder = fresh({"currentish-1.0.0.jar": "currentish-cf-1.0.0", "betaish-1.0.0-beta.jar": "betaish-cf-1.0.0-beta",
                    "gone-1.0.0.jar": "gone-cf-1.0.0", "dualish-1.0.0.jar": "dualish-cf-1.0.0"})
    with Fake() as fake, WithKey():
        res = run(folder, mc_version="1.20.1")
        none = run(folder)
    unc, cur, ups = by_file(res, "not_checked"), by_file(res, "up_to_date"), by_file(res, "updates")
    check("a release file whose only newer file is an alpha is up to date (default channel)", "currentish-1.0.0.jar" in cur, res)
    check("a beta file sees the newer release, not the newer alpha", ups["betaish-1.0.0-beta.jar"]["latest"]["file_id"] == "6102"
          and ups["betaish-1.0.0-beta.jar"]["latest"]["channel"] == "release", ups)
    check("an unavailable newest file is never offered: the file is up to date", "gone-1.0.0.jar" in cur, res)
    check("the loader came from the file itself (it names exactly one)", "currentish-1.0.0.jar" in cur and "betaish-1.0.0-beta.jar" in ups)
    check("a file for several Minecraft versions: the owner's answer picks one", ups["dualish-1.0.0.jar"]["latest"]["file_id"] == "5302", ups)
    check("nothing is in not_checked here", unc == {}, unc)
    unc2 = by_file(none, "not_checked")
    check("with no Minecraft version a file that lists two is a question, not a guess", unc2["dualish-1.0.0.jar"]["why"] == "needs_mc_version"
          and none["needs_clarification"] is True and "Minecraft version" in none["ask"], none.get("ask"))
    other = run(folder, mc_version="1.20.4")
    check("a file built for another Minecraft version is other_version, never up to date", {e["file"] for e in other["other_version"]} >= {"currentish-1.0.0.jar"}
          and "currentish-1.0.0.jar" not in by_file(other, "up_to_date"), other.get("other_version"))
    alpha = run(folder, mc_version="1.20.1", channel="alpha")
    check("channel alpha widens it: the alpha is now the newer file", by_file(alpha, "updates")["currentish-1.0.0.jar"]["latest"]["file_id"] == "5102", alpha)


def test_quilt_falls_back_to_fabric_and_never_guesses_a_loader():
    folder = fresh({"quiltish-1.0.0.jar": "quiltish-cf-1.0.0", "bothish-1.0.0.jar": "bothish-cf-1.0.0"})
    with Fake() as fake, WithKey():
        res = run(folder, mc_version="1.20.1", loader="quilt")
        off = run(folder, mc_version="1.20.1", loader="quilt", quilt_fallback=False)
        ask = run(folder, mc_version="1.20.1")
    ups = by_file(res, "updates")
    check("a Fabric-only file offers its newer Fabric file for Quilt, labelled via fabric", ups["quiltish-1.0.0.jar"]["latest"].get("via") == "fabric"
          and ups["quiltish-1.0.0.jar"]["latest"]["file_id"] == "5402", ups)
    check("a file tagged Fabric AND Quilt gets its newer file with no label", ups["bothish-1.0.0.jar"]["latest"]["file_id"] == "5502"
          and "via" not in ups["bothish-1.0.0.jar"]["latest"], ups)
    check("with the fallback off the Fabric-only file is other_version", [e["file"] for e in off["other_version"]] == ["quiltish-1.0.0.jar"], off)
    check("a file tagged with two loaders and no answer is needs_loader", by_file(ask, "not_checked")["bothish-1.0.0.jar"]["why"] == "needs_loader"
          and ask["needs_clarification"] is True and "loader" in ask["ask"], ask.get("ask"))
    lists = [c for c in fake.cf_calls() if re.match(r"^/v1/mods/\d+/files$", c["path"])]
    check("the fallback listing was fetched with the loader filter OFF (the vendor's filter is loose)",
          any(c["path"] == "/v1/mods/1005/files" and "modLoaderType" not in c["query"] for c in lists), [(c["path"], c["query"]) for c in lists])


def test_resource_packs_have_no_loader_and_a_mod_without_one_is_not_guessed():
    folder = fresh({"looky-cf-1.0.0.zip": "looky-cf-1.0.0", "loaderless-1.0.0.jar": "loaderless-cf-1.0.0"})
    with Fake() as fake, WithKey():
        res = run(folder, mc_version="1.20.1", loader="fabric")
    check("a resource pack is looked up by Minecraft version alone", by_file(res, "updates")["looky-cf-1.0.0.zip"]["latest"]["file_id"] == "5602", res)
    check("a MOD with no loader tag is not guessed at", by_file(res, "not_checked")["loaderless-1.0.0.jar"]["why"] == "no_loader"
          and "CurseForge" in by_file(res, "not_checked")["loaderless-1.0.0.jar"]["detail"], res["not_checked"])


# ------------------------------------------------------------------ never a guess

def test_an_exact_match_needs_fingerprint_and_sha1():
    folder = fresh({"collide.jar": "collide-fingerprint-bytes", "copy-a.jar": "shared-bytes-in-two-projects",
                    "fabricish-look-alike-1.0.0.jar": "bytes that merely look like fabricish"})
    with Fake() as fake, WithKey():
        res = run(folder, **FAB)
    unc = by_file(res, "not_checked")
    check("the same fingerprint with another SHA-1 is not used, and the answer says so", unc["collide.jar"]["why"] == "not_on_curseforge"
          and "another SHA-1" in unc["collide.jar"]["detail"], unc["collide.jar"])
    check("two different CurseForge files that are identical to it are ambiguous, never picked", unc["copy-a.jar"]["why"] == "ambiguous", unc["copy-a.jar"])
    check("a look-alike name led nowhere", unc["fabricish-look-alike-1.0.0.jar"]["why"] == "not_on_curseforge")
    check("nothing is checked, nothing offered", res["checked"] == 0 and res["updates"] == [], res)
    check("the lookup still ran and still says UNVERIFIED", res["curseforge"]["status"] == "checked" and "UNVERIFIED" in res["curseforge_note"])
    # a stray answer for a number that was not asked is dropped by the adapter; one for the right number with the wrong SHA-1 is not a match
    folder2 = fresh({"liar.jar": "fabricish-cf-1.0.0"})
    with Fake() as fake, WithKey():
        fake.fp_lies[fp("fabricish-cf-1.0.0")] = []
        res2 = run(folder2, **FAB)
    check("sanity: the honest case matches", "liar.jar" in by_file(res2, "updates"))


# ------------------------------------------------------------------ not distributable

def test_a_project_that_blocks_third_party_downloads_is_listed_not_routed_around():
    folder = fresh({"blockedish-1.0.0.jar": "blockedish-cf-1.0.0"})
    with Fake() as fake, WithKey():
        res = run(folder, mc_version="1.20.1", loader="forge")
    e = by_file(res, "not_checked")["blockedish-1.0.0.jar"]
    check("listed as not_distributable, with the project and its page and that a newer file exists", e["why"] == "not_distributable"
          and e["project"]["name"] == "Blockedish CF" and e["project"]["page_url"].startswith("https://www.curseforge.com/")
          and e["newer"]["date"] == "2026-02-01" and "doesn't allow third-party downloads" in e["detail"], e)
    check("it is not an update and has no get_with", res["updates"] == [] and "get_with" not in e)
    check("no download-url route was asked and no CDN host was contacted (not routed around)",
          not any("download-url" in c["path"] or "forgecdn" in c["url"] for c in fake.calls), [c["path"] for c in fake.calls])
    folder2 = fresh({"blockedish-1.1.0.jar": "blockedish-cf-1.1.0"})
    with Fake() as fake, WithKey():
        res2 = run(folder2, mc_version="1.20.1", loader="forge")
    check("a blocked project whose installed file is already the newest is simply up to date", "blockedish-1.1.0.jar" in by_file(res2, "up_to_date"), res2)


# ------------------------------------------------------------------ the unverified route

def _modrinth_half_reports(res):
    return [e["file"] for e in res["updates"]] == ["sodiumish-0.5.0.jar"] and res["checked"] == 1


def test_an_odd_or_changed_answer_marks_the_whole_curseforge_half_not_checked():
    scenarios = []
    for label, setup in (("a match for the fingerprint nobody has", lambda f: setattr(f, "odd_probe", True)),
                         ("an answer with no exactMatches", lambda f: setattr(f, "bad_shape", True)),
                         ("a refused route (404)", lambda f: f.cf_fail.update({"/fingerprints/": 404}))):
        folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "other.jar": "bytes nobody has", "sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
        spy, read = count_fingerprint_reads()
        with Fake() as fake, WithKey(), spy:
            setup(fake)
            res = run(folder, **FAB)
        unc = by_file(res, "not_checked")
        check("%s: ok true, never an error" % label, res.get("ok") is True and "error" not in res, res)
        check("%s: every CurseForge file is not checked with vendor_changed" % label,
              sorted(unc) == ["fabricish-1.0.0.jar", "other.jar"] and all(e["why"] == "vendor_changed" and e["source"] == "curseforge" for e in unc.values()), unc)
        check("%s: the Modrinth half still reports" % label, _modrinth_half_reports(res), res)
        check("%s: the block and the note say vendor_changed and UNVERIFIED" % label, res["curseforge"]["why"] == "vendor_changed"
              and res["curseforge"]["status"] == "skipped" and "UNVERIFIED" in res["curseforge_note"], res["curseforge"])
        check("%s: only the sanity question was asked and no file was read for a fingerprint" % label, len(fake.fp_calls()) == 1 and read == []
              and fake.fp_calls()[0]["json"] == {"fingerprints": [crossmatch_probe()]}, ([c["json"] for c in fake.fp_calls()], read))
        check("%s: no listing or project request followed" % label, [c for c in fake.cf_calls() if "/fingerprints/" not in c["path"]] == [])
        scenarios.append(res)
    check("the three scenarios said the same thing in the same words", len({json.dumps(r["curseforge_note"]) for r in scenarios}) == 1)


def test_a_real_answer_that_changes_after_a_good_sanity_check():
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
    calls = {"n": 0}
    with Fake() as fake, WithKey():
        real = fake._curseforge

        def second_answer_is_odd(method, path, query, body):
            if path == "/v1/fingerprints/432":
                calls["n"] += 1
                if calls["n"] == 2:
                    return fake._reply({"data": "unexpected"})
            return real(method, path, query, body)

        fake._curseforge = second_answer_is_odd
        res = run(folder, **FAB)
    check("a good sanity answer and a bad real one: the whole half is vendor_changed, Modrinth reports",
          res["curseforge"]["why"] == "vendor_changed" and by_file(res, "not_checked")["fabricish-1.0.0.jar"]["why"] == "vendor_changed"
          and _modrinth_half_reports(res), res)


def test_other_curseforge_failures_stay_inside_the_curseforge_half():
    for label, fail, why in (("a refused key", {"/fingerprints/": 403}, "bad_key"), ("rate limiting", {"/fingerprints/": 429}, "rate_limited"),
                             ("a server error", {"/fingerprints/": 500}, "offline")):
        folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
        with Fake() as fake, WithKey():
            fake.cf_fail.update(fail)
            res = run(folder, **FAB)
        e = by_file(res, "not_checked")["fabricish-1.0.0.jar"]
        check("%s: not an error, the file says %s, Modrinth still reports" % (label, why), res.get("ok") is True and e["why"] == why
              and res["curseforge"]["why"] == why and _modrinth_half_reports(res), res.get("curseforge"))
        check("%s: the note names the problem and says nothing was guessed" % label, "not checked" in res["curseforge_note"] and "guessed" in res["curseforge_note"]
              and FAKE_KEY not in json.dumps(res), res["curseforge_note"])
    # a failing listing for one project only hits that project's files
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "quiltish-1.0.0.jar": "quiltish-cf-1.0.0"})
    with Fake() as fake, WithKey():
        fake.cf_fail["/mods/1001/files"] = 404
        res = run(folder, **FAB)
    unc = by_file(res, "not_checked")
    check("a project that cannot be listed is not checked, its neighbour is", "fabricish-1.0.0.jar" in unc and unc["fabricish-1.0.0.jar"]["why"] == "not_found"
          and "quiltish-1.0.0.jar" in by_file(res, "updates"), res)
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "quiltish-1.0.0.jar": "quiltish-cf-1.0.0"})
    with Fake() as fake, WithKey():
        fake.cf_fail["/mods/1001/files"] = 429
        res = run(folder, **FAB)
    unc = by_file(res, "not_checked")
    check("rate limiting during the listings stops the rest of the half: the later file says so", res.get("ok") is True
          and unc["fabricish-1.0.0.jar"]["why"] == "rate_limited" and unc["quiltish-1.0.0.jar"]["why"] == "rate_limited"
          and res["curseforge"].get("stopped_by") == "rate_limited", res)


def test_nothing_is_asked_after_the_deadline():
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0"})
    flag = {"late": False}
    real_scan = updates.scan_folder

    def scan_then_late(*a, **k):
        out = real_scan(*a, **k)
        flag["late"] = True
        return out

    with Fake() as fake, WithKey(), Patch(updates, scan_folder=scan_then_late, _late=lambda d: flag["late"] and d is not None):
        res = run(folder, deadline_seconds=5, **FAB)
    check("a deadline passing after the scan: partial, no request at all (the file is offline, not 'unknown to Modrinth')",
          res["partial"] is True and fake.calls == [] and by_file(res, "not_checked")["fabricish-1.0.0.jar"]["why"] == "offline", res)


# ------------------------------------------------------------------ the fingerprint cache

def test_the_fingerprint_is_remembered_next_to_the_sha1():
    wipe_state()
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "sodiumish-0.5.0.jar": "sodiumish-0.5.0"})
    spy, read = count_fingerprint_reads()
    hashed, real_hash = [], updates._hash_file
    with Fake(), WithKey(), spy, Patch(updates, _hash_file=lambda p: hashed.append(os.path.basename(p)) or real_hash(p)):
        run(folder, **FAB)
        check("first run: the unknown file was fingerprinted, the Modrinth one was not", read == ["fabricish-1.0.0.jar"], read)
        data = json.loads(state_file().read_text(encoding="utf-8"))
        entries = {os.path.basename(k): v for k, v in data["hashes"].items()}
        check("the fingerprint sits next to the sha1: [size, mtime_ns, sha1, fingerprint]", len(entries["fabricish-1.0.0.jar"]) == 4
              and entries["fabricish-1.0.0.jar"][2] == sha1("fabricish-cf-1.0.0") and entries["fabricish-1.0.0.jar"][3] == fp("fabricish-cf-1.0.0"), entries)
        check("a file that was never asked about keeps three values", len(entries["sodiumish-0.5.0.jar"]) == 3)
        text = state_file().read_text(encoding="utf-8")
        check("no key, no vendor answer, no project name in the state file", FAKE_KEY not in text and "Fabricish" not in text and "1001" not in text
              and "5002" not in text and "exactMatches" not in text)
        read.clear()
        hashed.clear()
        run(folder, **FAB)
        check("second run: nothing is hashed and nothing is fingerprinted again", read == [] and hashed == [], (read, hashed))
        (folder / "fabricish-1.0.0.jar").write_bytes(b"changed bytes, another size entirely")
        run(folder, **FAB)
        check("a changed file is hashed again and its old fingerprint is not reused", hashed == ["fabricish-1.0.0.jar"] and read == ["fabricish-1.0.0.jar"], (hashed, read))
        data = json.loads(state_file().read_text(encoding="utf-8"))
        changed = [v for k, v in data["hashes"].items() if k.endswith("fabricish-1.0.0.jar")][0]
        check("and the new fingerprint is the new bytes' own", changed[3] == crossmatch.cf_fingerprint(b"changed bytes, another size entirely"), changed)


def test_state_entries_old_and_damaged():
    wipe_state()
    good = "a" * 40
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps({"v": 1, "last_folder": None, "hashes": {
        "old": [5, 6, good], "new": [5, 6, good, 123456], "bad_fp": [5, 6, good, "not a number"], "float_fp": [5, 6, good, 1.5],
        "huge_fp": [5, 6, good, 2 ** 40], "bool_fp": [5, 6, good, True], "five": [5, 6, good, 1, 2], "bad_sha": [5, 6, "xyz", 1]}}), encoding="utf-8")
    loaded = updates.load_state()["hashes"]
    check("an old three-value entry still loads", loaded["old"] == [5, 6, good])
    check("a good four-value entry loads", loaded["new"] == [5, 6, good, 123456])
    check("a damaged fingerprint is dropped but the sha1 stays", loaded["bad_fp"] == [5, 6, good] and loaded["float_fp"] == [5, 6, good]
          and loaded["huge_fp"] == [5, 6, good] and loaded["bool_fp"] == [5, 6, good], loaded)
    check("a wrong length or a bad sha1 is dropped whole", "five" not in loaded and "bad_sha" not in loaded, loaded)
    check("saving keeps four values and says v 2", updates.save_state({"last_folder": "x", "hashes": {"k": [1, 2, good, 7]}})
          and json.loads(state_file().read_text(encoding="utf-8"))["hashes"]["k"] == [1, 2, good, 7]
          and json.loads(state_file().read_text(encoding="utf-8"))["v"] == 2)


def test_a_file_that_changes_under_the_fingerprint_pass_is_not_trusted():
    wipe_state()
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0"})
    real = crossmatch.fingerprint_file
    # the file's bytes change between the SHA-1 pass and the fingerprint pass: the two SHA-1s differ, so no fingerprint is used
    swapped = lambda p: dict(real(p), sha1="0" * 40)       # noqa: E731
    with Fake() as fake, WithKey(), Patch(crossmatch, fingerprint_file=swapped):
        res = run(folder, **FAB)
    e = by_file(res, "not_checked")["fabricish-1.0.0.jar"]
    check("it is not checked (unreadable), never matched, and no real fingerprint went out", e["why"] == "unreadable" and len(fake.fp_calls()) == 1, (e, [c["json"] for c in fake.fp_calls()]))
    check("and nothing was remembered for it", all(len(v) == 3 for v in json.loads(state_file().read_text(encoding="utf-8"))["hashes"].values()))


# ------------------------------------------------------------------ the safe default folder

def test_no_folder_and_no_last_folder_asks_instead_of_reading_downloads():
    wipe_state()
    downloads = HOMEP / "Downloads"
    downloads.mkdir(exist_ok=True)
    (downloads / "fabricish-1.0.0.jar").write_bytes(b"fabricish-cf-1.0.0")
    hashed, real_hash = [], updates._hash_file
    spy, read = count_fingerprint_reads()
    with Fake() as fake, WithKey(), spy, Patch(updates, _hash_file=lambda p: hashed.append(p) or real_hash(p)):
        res = updates.check_folder(folder=None, default_folder=str(downloads))
        check("needs_folder, ok, asking", res.get("ok") is True and res["needs_folder"] is True and res["needs_clarification"] is True, res)
        check("the ask line names default_dest and the folder as the suggestion", str(downloads) in res["ask"] and "default_dest" in res["ask"]
              and res["suggested_folder"] == str(downloads) and res["suggestion_from"] == "default_dest", res["ask"])
        check("the message says nothing was read, hashed, sent or changed", "Nothing was read, hashed, sent or changed" in res["message"] and res["nothing_changed"] is True)
        check("the usual lists are there and empty", res["checked"] == 0 and res["updates"] == [] and res["up_to_date"] == [] and res["not_checked"] == [])
        check("nothing was hashed, fingerprinted or requested, and no state was written", hashed == [] and read == [] and fake.calls == [] and not state_file().exists())
        for blank in ("", "   ", None):
            check("a blank folder %r counts as no folder" % (blank,), updates.check_folder(folder=blank, default_folder=str(downloads)).get("needs_folder") is True)
        check("the tool says the same (default_dest ~ expanded)", _tool_with_default("~/Downloads", {}).get("suggested_folder") == str(downloads))
        named = updates.check_folder(folder=str(downloads), default_folder=str(downloads), **FAB)
        check("naming the folder checks it", named.get("ok") is True and named["folder_from"] == "given" and named["checked"] == 1, named)
        again = updates.check_folder(folder=None, default_folder=str(downloads), **FAB)
        check("and then it is the last folder checked: no question any more", again.get("folder_from") == "last_checked" and again["checked"] == 1, again)
    # a default that is not usable is not suggested
    wipe_state()
    res = updates.check_folder(folder=None, default_folder=str(HOMEP))
    check("a default that is protected (the home folder) is not suggested, and the ask says why", res.get("needs_folder") is True and "suggested_folder" not in res
          and "can't be used" in res["ask"], res)
    res = updates.check_folder(folder=None, default_folder=None)
    check("no default at all falls back to the stock suggestion and still never reads anything", res.get("ok") is True and res.get("needs_folder") is True, res)


def _tool_with_default(value, args):
    with Patch(mu, _setting=lambda key, default: {"default_dest": value}.get(key, default)):
        return mu.tool_check_mc_updates(args)


# ------------------------------------------------------------------ guests, hygiene, shaping

def test_a_guest_is_refused_before_any_curseforge_work():
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0"})
    spy, read = count_fingerprint_reads()
    with Fake() as fake, WithKey(), spy:
        old = os.environ.get("JARVIS_CHANNEL_SENDER")
        os.environ["JARVIS_CHANNEL_SENDER"] = GUEST
        try:
            res = mu.tool_check_mc_updates({"folder": str(folder), **FAB})
        finally:
            if old is None:
                os.environ.pop("JARVIS_CHANNEL_SENDER", None)
            else:
                os.environ["JARVIS_CHANNEL_SENDER"] = old
    check("a chat guest is refused, nothing asked, nothing read", res.get("ok") is False and fake.calls == [] and read == [], res)


def test_source_hygiene():
    src = Path(updates.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"net\.fetch\(([^)]*)\)", src)
    check("updates.py asks net only for Modrinth: CurseForge goes through the adapter", calls and all('"modrinth"' in c for c in calls) and "curseforge" not in "".join(calls), calls)
    check("updates.py never calls get_key() or names the key file or the key variable", "get_key" not in src and "curseforge.json" not in src and "CURSEFORGE_API_KEY" not in src)
    check("it opens no connection of its own and never reads x-api-key", not re.search(r"\b(requests|socket|urllib\.request|http\.client)\b", src) and "x-api-key" not in src.lower())
    check("it reaches CurseForge only through sources.get('curseforge')", "sources.get(\"curseforge\")" in src)
    check("it reuses crossmatch's own fingerprint function rather than a copy", "fingerprint_file" in src and "def cf_fingerprint" not in src and "_Murmur2" not in src)
    check("the module still has no way to delete, move or copy a file", "os.remove" not in src and "os.unlink" not in src and "shutil" not in src and "os.rename" not in src)
    action = Path(mu.__file__).read_text(encoding="utf-8")
    check("the action file does not touch the key either", "get_key" not in action and "curseforge.json" not in action)


def test_the_answer_survives_shaping_and_marks_vendor_text():
    folder = fresh({"fabricish-1.0.0.jar": "fabricish-cf-1.0.0", "blockedish-1.0.0.jar": "blockedish-cf-1.0.0", "x.jar": "unknown bytes"})
    with Fake() as fake, WithKey():
        res = run(folder, **FAB)
    for level in ("medium", "low"):
        shaped = tool_result_shaping.shape_result("check_mc_updates", copy.deepcopy(res), level)
        check("%s keeps the CurseForge block, its note and every CurseForge entry's get_with" % level,
              all(k in shaped for k in ("curseforge", "curseforge_note", "message", "nothing_changed"))
              and all("get_with" in u for u in shaped["updates"]), sorted(shaped))
    check("vendor text from CurseForge is marked untrusted", res.get("untrusted_note", "").startswith(untrusted.NOTE), res.get("untrusted_note"))
    needs = {"ok": True, "needs_folder": True, "needs_clarification": True, "ask": "Which folder?", "suggested_folder": "x", "message": "m", "nothing_changed": True}
    shaped = tool_result_shaping.shape_result("check_mc_updates", copy.deepcopy(needs), "low")
    check("a needs_folder answer keeps its question at every capacity", all(k in shaped for k in ("ask", "needs_folder", "suggested_folder", "message")), shaped)
    drops = [x for level in (mu.TOOL_RESULT_SPECS["check_mc_updates"].get("drop_fields") or {}).values() for x in level]
    check("the spec never drops the CurseForge block or the question", not ({"curseforge", "curseforge_note", "ask", "needs_folder", "suggested_folder"} & set(drops)), drops)


def test_the_checklist_and_schema_describe_the_new_behaviour():
    entry = mu.TEST_CHECKLIST["check_mc_updates"]
    blob = json.dumps(entry).lower()
    check("the checklist mentions the CurseForge fingerprint, the key hint, not_distributable and needs_folder",
          "fingerprint" in blob and "no_key" in blob and "not_distributable" in blob and "needs_folder" in blob and "unverified" in blob)
    desc = mu.TOOL_SCHEMAS[0]["description"]
    check("the tool description keeps its promises and adds the new ones", "Read-only" in desc and "never 'updated'" in desc and "download_mc_file" in desc
          and "UNVERIFIED" in desc and "needs_folder" in desc and "not_distributable" in desc)
    from jarvis import checklist_schema
    check("the checklist entry still passes the schema", checklist_schema.validate_entry("check_mc_updates", entry) == [], checklist_schema.validate_entry("check_mc_updates", entry))


# --- runner (keep BELOW every test: it reads globals() when it executes) ----

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
