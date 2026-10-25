"""jarvis/modhosts/resolve.py: the rules of plan section 11, table-tested.

Covers: normalise_mc_version (1.21.11 AND 26.3 / 26.1.2 shapes) and normalise_loader; pick_file (exact Minecraft
version, strict loaders, release > beta > alpha, newest wins, a pinned file always wins, the `unfiltered` flag, server
packs skipped, the Quilt-to-Fabric fallback labelled `via`, `no_compatible_file` with the closest alternatives);
resolve_project (the never-guess rule); add_dependencies (dedupe by project, cycles broken, depth cap, pinned
dependency files, a failed bulk call becomes `skipped`).

No network: the project / dependency tests use the in-process fake Modrinth or a tiny fake adapter.
Run: python tests/test_modhosts_resolve.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-modhosts-resolve-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_CHANNEL_SENDER", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from _modhosts_fake_servers import FakeModrinth  # noqa: E402
from jarvis.modhosts import cache, modrinth, resolve, sources  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


def F(fid, mc, loaders, channel="release", date="2024-01-01T00:00:00Z", deps=None, pid="P1", **extra):
    out = {"source": "test", "project_id": pid, "file_id": fid, "file_name": fid + ".jar", "version_label": fid,
           "mc_versions": list(mc) if isinstance(mc, (list, tuple)) else [mc], "loaders": list(loaders),
           "channel": channel, "date": date, "size": 1, "download_url": "https://cdn.modrinth.com/x",
           "downloadable": True, "hashes": {}, "dependencies": deps or []}
    out.update(extra)
    return out


def ids(files):
    return [f["file_id"] for f in files]


class PatchedSources:
    """Makes sources.get() answer with `adapter` for any source (restores it afterwards)."""

    def __init__(self, adapter):
        self.adapter = adapter

    def __enter__(self):
        self.old = sources.get
        sources.get = lambda name: self.adapter
        return self

    def __exit__(self, *exc):
        sources.get = self.old


class Adapter:
    """A tiny adapter: the functions the code under test calls, answering from dicts."""

    def __init__(self, files=None, projects=None, search_hits=None, pinned=None, bulk_error=None):
        self.files, self.projects, self.search_hits = files or {}, projects or {}, search_hits or []
        self.pinned, self.bulk_error = pinned or {}, bulk_error
        self.list_calls = []

    def get_project(self, ref):
        if isinstance(ref, str) and ref == "boom":
            raise RuntimeError("adapter blew up")
        project = self.projects.get(ref)
        return {"ok": True, "project": project} if project else {"ok": False, "reason": "not_found", "error": "no"}

    def search(self, query, kind="mod", mc_version=None, loader=None, sort="relevance", limit=5, offset=0):
        return {"ok": True, "hits": list(self.search_hits), "total": len(self.search_hits)}

    def bulk_list_files(self, project_ids, mc_version=None, loader=None):
        self.list_calls.append((list(project_ids), mc_version, loader))
        if self.bulk_error:
            return {"ok": False, "reason": self.bulk_error, "error": "x"}
        return {"ok": True, "files": {p: list(self.files.get(p, [])) for p in project_ids}}

    def bulk_get_files(self, pairs):
        return {"ok": True, "files": {"%s:%s" % (p, f): self.pinned.get(f) for p, f in pairs}}


# ------------------------------------------------------------------ normalisers

def test_normalise_mc_version():
    good = {"1.21.11": "1.21.11", "26.3": "26.3", "26.1.2": "26.1.2", "1.20": "1.20", "1.20.1": "1.20.1",
            " 1.20.1 ": "1.20.1", "v26.3": "26.3", "Minecraft 1.21.1": "1.21.1", "mc1.19.4": "1.19.4",
            "26.1": "26.1", "1.21.0": "1.21.0"}
    for raw, want in good.items():
        check("accepts %r" % raw, resolve.normalise_mc_version(raw) == want, resolve.normalise_mc_version(raw))
    bad = ["", "1", "latest", "1.20.x", "1.20.1.1", "1.2.3.4", "a.b", "24w14a", "1.20-pre1", "01.2", "1..2", "1.", ".1",
           "1.20.1; rm -rf", None, 26.3, 1, ["1.20.1"]]
    for raw in bad:
        check("rejects %r" % (raw,), resolve.normalise_mc_version(raw) is None, resolve.normalise_mc_version(raw))


def test_normalise_loader():
    good = {"fabric": "fabric", "Fabric": "fabric", " FABRIC ": "fabric", "fabric-loader": "fabric", "forge": "forge",
            "Minecraft Forge": "forge", "neoforge": "neoforge", "Neo Forge": "neoforge", "neo-forge": "neoforge",
            "NeoForged": "neoforge", "quilt": "quilt", "Quilt Loader": "quilt"}
    for raw, want in good.items():
        check("loader %r -> %s" % (raw, want), resolve.normalise_loader(raw) == want)
    for raw in ["", "liteloader", "cauldron", "vanilla", "forge fabric", None, 4, ["fabric"]]:
        check("loader %r -> None" % (raw,), resolve.normalise_loader(raw) is None)
    check("Forge and NeoForge are different loaders", resolve.normalise_loader("forge") != resolve.normalise_loader("neoforge"))


def test_version_order_helpers():
    check("26.3 is newer than 1.21.11", resolve.version_key("26.3") > resolve.version_key("1.21.11"))
    check("1.21.11 is newer than 1.21.2 (numeric, not text)", resolve.version_key("1.21.11") > resolve.version_key("1.21.2"))
    check("two parts count as x.y.0", resolve.version_key("26.3") == (26, 3, 0))
    check("junk sorts last", resolve.version_key("24w14a") == (0, 0, 0))


# ------------------------------------------------------------------ pick_file

def test_pick_file_exact_version_and_loader():
    files = [F("old", "1.20.1", ["fabric"], date="2024-01-01T00:00:00Z"), F("new", "1.20.1", ["fabric"], date="2024-06-01T00:00:00Z"),
             F("other_mc", "1.20.4", ["fabric"], date="2025-01-01T00:00:00Z"), F("forge", "1.20.1", ["forge"], date="2025-02-01T00:00:00Z")]
    res = resolve.pick_file(files, "1.20.1", "fabric", quilt_fallback=True)
    check("the newest exact match wins", res["ok"] and res["file"]["file_id"] == "new" and res["unfiltered"] is False, res)
    miss = resolve.pick_file(files, "1.20", "fabric", quilt_fallback=True)
    check("1.20 is not 1.20.1: strings, never ranges", miss["ok"] is False and miss["reason"] == "no_compatible_file", miss)
    check("a 26.3 style version matches exactly",
          resolve.pick_file([F("a", "26.3", ["fabric"]), F("b", "26.1.2", ["fabric"])], "26.3", "fabric", quilt_fallback=True)["file"]["file_id"] == "a")
    multi = resolve.pick_file([F("m", ["1.20.1", "1.20.2"], ["fabric"])], "1.20.2", "fabric", quilt_fallback=True)
    check("a file listing several Minecraft versions matches any of them", multi["ok"], multi)


def test_pick_file_loaders_are_strict():
    forge_only = [F("f", "1.21.1", ["forge"])]
    res = resolve.pick_file(forge_only, "1.21.1", "neoforge", quilt_fallback=True)
    check("NeoForge is not offered a Forge file", res["ok"] is False and res["reason"] == "no_compatible_file", res)
    check("...and the Forge file is listed as an alternative", ids(res["alternatives"]) == ["f"], res)
    check("Forge is not offered a NeoForge file", resolve.pick_file([F("n", "1.21.1", ["neoforge"])], "1.21.1", "forge")["ok"] is False)
    check("a file for several loaders matches each", all(
        resolve.pick_file([F("x", "1.21.1", ["fabric", "forge"])], "1.21.1", l)["ok"] for l in ("fabric", "forge")))


def test_pick_file_channels():
    rel_old = F("rel", "1.20.1", ["fabric"], "release", "2024-01-01T00:00:00Z")
    beta_new = F("beta", "1.20.1", ["fabric"], "beta", "2024-06-01T00:00:00Z")
    alpha_newest = F("alpha", "1.20.1", ["fabric"], "alpha", "2024-09-01T00:00:00Z")
    both = [alpha_newest, beta_new, rel_old]
    for channel in ("release", "beta", "alpha"):
        res = resolve.pick_file(both, "1.20.1", "fabric", channel=channel, quilt_fallback=True)
        check("release is preferred to a newer beta / alpha (channel=%s)" % channel, res["ok"] and res["file"]["file_id"] == "rel", res)
    only_beta = [beta_new, alpha_newest]
    res = resolve.pick_file(only_beta, "1.20.1", "fabric", channel="release", quilt_fallback=True)
    check("channel=release never hands out a beta", res["ok"] is False and res["reason"] == "no_compatible_file", res)
    check("...and says a less stable channel has files", "less stable" in res["error"] and "beta" in ids(res["alternatives"]), res)
    res = resolve.pick_file(only_beta, "1.20.1", "fabric", channel="beta", quilt_fallback=True)
    check("channel=beta accepts a beta (and still not an alpha)", res["ok"] and res["file"]["file_id"] == "beta", res)
    res = resolve.pick_file([alpha_newest], "1.20.1", "fabric", channel="beta", quilt_fallback=True)
    check("channel=beta refuses an alpha", res["ok"] is False)
    res = resolve.pick_file([alpha_newest], "1.20.1", "fabric", channel="alpha", quilt_fallback=True)
    check("channel=alpha accepts it", res["ok"] and res["file"]["file_id"] == "alpha")
    res = resolve.pick_file([rel_old], "1.20.1", "fabric", channel="nonsense", quilt_fallback=True)
    check("an unknown channel is treated as release", res["ok"] and res["file"]["file_id"] == "rel")


def test_pick_file_dates_pinned_and_server_packs():
    mixed = [F("z", "1.20.1", ["fabric"], date="2024-06-01T10:00:00Z"), F("offset", "1.20.1", ["fabric"], date="2024-06-01T12:00:00+02:00"),
             F("later", "1.20.1", ["fabric"], date="2024-06-01T10:30:00.123456+00:00")]
    check("dates in different ISO spellings compare as times", resolve.pick_file(mixed, "1.20.1", "fabric")["file"]["file_id"] == "later")
    check("a file with no date loses to one with a date",
          resolve.pick_file([F("nodate", "1.20.1", ["fabric"], date=""), F("dated", "1.20.1", ["fabric"])], "1.20.1", "fabric")["file"]["file_id"] == "dated")

    files = [F("a", "1.20.1", ["fabric"]), F("pin", "1.19.2", ["forge"], channel="alpha", date="2020-01-01T00:00:00Z")]
    res = resolve.pick_file(files, "1.20.1", "fabric", pinned="pin")
    check("a pinned file always wins, whatever it is", res["ok"] and res["file"]["file_id"] == "pin", res)
    check("a pinned id may be an int", resolve.pick_file([F("7", "1.20.1", ["fabric"])], pinned=7)["ok"])
    res = resolve.pick_file(files, "1.20.1", "fabric", pinned="ghost")
    check("a pinned id that is not there fails", res["ok"] is False and res["reason"] == "no_compatible_file", res)

    server = [F("srv", "1.20.1", ["fabric"], date="2025-01-01T00:00:00Z", is_server_pack=True), F("cli", "1.20.1", ["fabric"])]
    check("a server pack is never chosen", resolve.pick_file(server, "1.20.1", "fabric")["file"]["file_id"] == "cli")
    check("...nor listed", ids(resolve.filter_files(server, "1.20.1", "fabric")) == ["cli"])


def test_pick_file_unfiltered_flag():
    files = [F("a", "1.20.1", ["fabric"], date="2024-01-01T00:00:00Z"), F("b", "1.21.1", ["forge"], date="2025-01-01T00:00:00Z")]
    both = resolve.pick_file(files)
    check("no filters: the newest file overall, flagged unfiltered", both["ok"] and both["file"]["file_id"] == "b" and both["unfiltered"] is True, both)
    only_mc = resolve.pick_file(files, mc_version="1.20.1")
    check("only mc_version: filtered on it, still flagged unfiltered (loader missing)", only_mc["file"]["file_id"] == "a" and only_mc["unfiltered"] is True, only_mc)
    only_loader = resolve.pick_file(files, loader="forge")
    check("only loader: likewise", only_loader["file"]["file_id"] == "b" and only_loader["unfiltered"] is True, only_loader)
    full = resolve.pick_file(files, "1.20.1", "fabric", quilt_fallback=True)
    check("both given: not unfiltered", full["unfiltered"] is False)
    check("an empty list is no_compatible_file, not a crash", resolve.pick_file([])["reason"] == "no_compatible_file")
    check("junk in the list is ignored", resolve.pick_file([None, "x", F("ok", "1.20.1", ["fabric"])], "1.20.1", "fabric")["ok"])


def test_pick_file_quilt_falls_back_to_fabric():
    fabric = F("fab", "1.20.1", ["fabric"], date="2024-06-01T00:00:00Z")
    quilt = F("qui", "1.20.1", ["quilt"], date="2024-01-01T00:00:00Z")
    res = resolve.pick_file([fabric, quilt], "1.20.1", "quilt", quilt_fallback=True)
    check("a native Quilt file beats a newer Fabric one, with no via", res["file"]["file_id"] == "qui" and "via" not in res["file"], res)
    res = resolve.pick_file([fabric], "1.20.1", "quilt", quilt_fallback=True)
    check("no Quilt file: the Fabric one, marked via fabric", res["ok"] and res["file"]["file_id"] == "fab" and res["file"]["via"] == "fabric", res)
    check("the caller's file dict is not changed", "via" not in fabric)
    res = resolve.pick_file([fabric], "1.20.1", "quilt", quilt_fallback=False)
    check("fallback off: no_compatible_file", res["ok"] is False and res["reason"] == "no_compatible_file", res)
    both_tags = F("cf", "1.20.1", ["fabric", "quilt"])
    res = resolve.pick_file([both_tags], "1.20.1", "quilt", quilt_fallback=True)
    check("a file tagged Fabric and Quilt (CurseForge does this) is native Quilt", res["ok"] and "via" not in res["file"], res)
    check("Fabric is never served a Quilt-only file", resolve.pick_file([quilt], "1.20.1", "fabric", quilt_fallback=True)["ok"] is False)
    check("the fallback is for Quilt only (Forge gets no Fabric file)", resolve.pick_file([fabric], "1.20.1", "forge", quilt_fallback=True)["ok"] is False)
    check("filter_files follows the same rule", ids(resolve.filter_files([fabric, quilt], "1.20.1", "quilt", quilt_fallback=True)) == ["qui"]
          and resolve.filter_files([fabric], "1.20.1", "quilt", quilt_fallback=True)[0]["via"] == "fabric")
    miss = resolve.pick_file([F("fab19", "1.19.2", ["fabric"])], "1.20.1", "quilt", quilt_fallback=True)
    check("an alternative that would be served through the fallback says via", miss["alternatives"][0].get("via") == "fabric", miss)


def test_pick_file_quilt_fallback_reads_the_tool_setting():
    fabric = F("fab", "1.20.1", ["fabric"])
    settings = Path(HOME) / ".jarvis" / "tool_settings.json"
    check("default (nothing stored): fallback on", resolve.pick_file([fabric], "1.20.1", "quilt")["ok"] is True)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"minecraft_lookup": {"quilt_fabric_fallback": False}}), encoding="utf-8")
    try:
        check("setting off: no fallback", resolve.pick_file([fabric], "1.20.1", "quilt")["ok"] is False)
        check("an explicit argument beats the setting", resolve.pick_file([fabric], "1.20.1", "quilt", quilt_fallback=True)["ok"] is True)
    finally:
        settings.unlink()
    check("setting removed: on again", resolve.pick_file([fabric], "1.20.1", "quilt")["ok"] is True)


def test_pick_file_alternatives_are_the_closest():
    files = [F("v119", "1.19.2", ["fabric"]), F("v121", "1.21", ["fabric"]), F("v1204", "1.20.4", ["fabric"]),
             F("forge", "1.20.1", ["forge"]), F("v26", "26.3", ["fabric"])]
    res = resolve.pick_file(files, "1.20.1", "fabric", quilt_fallback=True)
    check("no_compatible_file", res["ok"] is False and res["reason"] == "no_compatible_file" and res["error"], res)
    alts = ids(res["alternatives"])
    check("the nearest Minecraft version (same loader) comes first", alts[0] == "v1204", alts)
    check("the right loader is listed before the wrong one", alts.index("forge") > alts.index("v121") and alts.index("forge") > alts.index("v119"), alts)
    check("at most the alternatives cap", len(alts) <= resolve.ALTERNATIVES and len(set(alts)) == len(alts), alts)
    near = resolve.pick_file([F("a", "26.1.2", ["fabric"]), F("b", "1.21.11", ["fabric"])], "26.3", "fabric", quilt_fallback=True)
    check("26.3 is nearest to 26.1.2, not 1.21.11", ids(near["alternatives"])[0] == "a", near)
    check("a duplicate file id is listed once", len(ids(resolve.pick_file([F("d", "1.0", ["forge"]), F("d", "1.0", ["forge"])], "1.20.1", "fabric")["alternatives"])) == 1)


# ------------------------------------------------------------------ resolve_project (never guess)

def test_resolve_project_never_guesses():
    cache.clear()
    fake = FakeModrinth().install()
    try:
        res = resolve.resolve_project("modrinth", "sodium")
        check("an exact slug wins", res["ok"] and res["project"]["slug"] == "sodium", res)
        check("an exact id wins", resolve.resolve_project("modrinth", "AANobbMI")["project"]["slug"] == "sodium")
        check("an exact slug wins even when another project has the same name",
              resolve.resolve_project("modrinth", "twin-mod")["project"]["id"] == "TWIN0001")
        res = resolve.resolve_project("modrinth", "Totally Normal Mod")
        check("the only result whose normalised name equals the query wins", res["ok"] and res["project"]["id"] == "EVIL0001", res)
        res = resolve.resolve_project("modrinth", "twin mod")
        check("two projects with that name: ambiguous, candidates returned",
              res["ok"] is False and res["reason"] == "ambiguous" and {c["id"] for c in res["candidates"]} == {"TWIN0001", "TWIN0002"}, res)
        res = resolve.resolve_project("modrinth", "Fabric")
        check("several loose matches and no exact name: ambiguous", res["reason"] == "ambiguous" and 1 < len(res["candidates"]) <= resolve.NAME_CANDIDATES, res)
        check("...and never the top hit", "project" not in res)
        res = resolve.resolve_project("modrinth", "zzzqqq nothing at all")
        check("nothing matches: not_found with no candidates", res["reason"] == "not_found" and res["candidates"] == [], res)
        check("an empty name is not_found", resolve.resolve_project("modrinth", "  ")["reason"] == "not_found")
        fake.fail_paths["/project/"] = (503, {})
        cache.clear()
        res = resolve.resolve_project("modrinth", "never-seen-slug")
        check("a vendor outage is passed through, not turned into a search or a guess", res["ok"] is False and res["reason"] == "offline", res)
    finally:
        fake.uninstall()


def test_resolve_project_with_an_adapter_and_edge_cases():
    one_loose = Adapter(search_hits=[{"id": "1", "name": "Something Else", "slug": "else"}])
    with PatchedSources(one_loose):
        res = resolve.resolve_project("modrinth", "my mod")
        check("a single non-exact hit is still ambiguous (never guess)", res["reason"] == "ambiguous" and len(res["candidates"]) == 1, res)
    exact = Adapter(search_hits=[{"id": "1", "name": "My-Mod!", "slug": "a"}, {"id": "2", "name": "Other", "slug": "b"}])
    with PatchedSources(exact):
        res = resolve.resolve_project("modrinth", "my mod")
        check("names are compared with case and punctuation ignored", res["ok"] and res["project"]["id"] == "1", res)
    boom = Adapter()
    with PatchedSources(boom):
        res = resolve.resolve_project("modrinth", "boom")
        check("an adapter that raises becomes vendor_changed, never an exception", res["ok"] is False and res["reason"] == "vendor_changed" and res["candidates"] == [], res)
    with PatchedSources(None):
        res = resolve.resolve_project("curseforge", "x")
        check("a source that is not installed is unsupported with one plain sentence",
              res["reason"] == "unsupported" and "not installed" in res["error"], res)
        check("an unknown source is unsupported", resolve.resolve_project("somewhere", "x")["reason"] == "unsupported")


# ------------------------------------------------------------------ add_dependencies

def req(pid, fid=None):
    return {"project_id": pid, "file_id": fid, "type": "required"}


def test_add_dependencies_with_the_fake_modrinth():
    cache.clear()
    fake = FakeModrinth().install()
    try:
        sodium = modrinth.list_files("sodium", "1.20.1", "fabric")["files"]
        base = [f for f in sodium if f["file_id"] == "vSod0001"]
        before = json.dumps(base, sort_keys=True)
        res = resolve.add_dependencies("modrinth", base, "1.20.1", "fabric")
        check("the required dependency is added (newest release for the version)", res["ok"] and ids(res["added"]) == ["vFab0003"], res)
        check("optional, embedded and incompatible ones are not added", res["skipped"] == [], res)
        check("the cycle (fabric-api needs sodium) is broken: sodium is not added again", all(f["project_id"] != "AANobbMI" for f in res["added"]))
        check("the files passed in are not changed", json.dumps(base, sort_keys=True) == before)

        both = [f for f in sodium if f["file_id"] in ("vSod0001", "vSod0002")]
        res = resolve.add_dependencies("modrinth", both, "1.20.1", "fabric")
        check("two files needing the same project add it once", ids(res["added"]) == ["vFab0003"], res)

        res = resolve.add_dependencies("modrinth", base, "1.19.2", "fabric")
        check("a dependency with no compatible file is skipped with a reason, not a failure",
              res["ok"] and res["added"] == [] and res["skipped"] == [{"project_id": "P7dR8mSH", "reason": "no_compatible_file"}], res)

        fake.calls.clear()
        cache.clear()
        res = resolve.add_dependencies("modrinth", base, "1.20.1", "quilt")
        query = [c for c in fake.calls if c["path"] == "/v2/project/P7dR8mSH/version"][-1]["query"]
        check("for Quilt the dependency files are fetched with the loader filter OFF", "loaders" not in query and ids(res["added"]) == ["vFab0003"], (query, res))
        check("an empty list is ok and empty", resolve.add_dependencies("modrinth", [], "1.20.1", "fabric") == {"ok": True, "added": [], "skipped": []})
    finally:
        fake.uninstall()


def test_add_dependencies_depth_cycles_pins_and_failures():
    chain = {"B": [F("b1", "1.20.1", ["fabric"], pid="B", deps=[req("C")])],
             "C": [F("c1", "1.20.1", ["fabric"], pid="C", deps=[req("D")])],
             "D": [F("d1", "1.20.1", ["fabric"], pid="D")]}
    start = [F("a1", "1.20.1", ["fabric"], pid="A", deps=[req("B")])]
    with PatchedSources(Adapter(files=chain)):
        res = resolve.add_dependencies("modrinth", start, "1.20.1", "fabric")
        check("a chain is followed to the end", ids(res["added"]) == ["b1", "c1", "d1"] and res["skipped"] == [], res)
        res = resolve.add_dependencies("modrinth", start, "1.20.1", "fabric", max_depth=2)
        check("the depth cap stops the chain and says so", ids(res["added"]) == ["b1", "c1"]
              and res["skipped"] == [{"project_id": "D", "reason": "depth_limit"}], res)

    loop = {"B": [F("b1", "1.20.1", ["fabric"], pid="B", deps=[req("A")])]}
    with PatchedSources(Adapter(files=loop)):
        res = resolve.add_dependencies("modrinth", [F("a1", "1.20.1", ["fabric"], pid="A", deps=[req("B")])], "1.20.1", "fabric")
        check("A needs B needs A: B is added once, no endless loop", ids(res["added"]) == ["b1"] and res["skipped"] == [], res)
    selfdep = [F("s1", "1.20.1", ["fabric"], pid="S", deps=[req("S")])]
    with PatchedSources(Adapter()):
        check("a project that requires itself adds nothing", resolve.add_dependencies("modrinth", selfdep, "1.20.1", "fabric")["added"] == [])

    pinned = {"vPIN": F("vPIN", "1.20.1", ["fabric"], pid="B")}
    with PatchedSources(Adapter(pinned=pinned)):
        res = resolve.add_dependencies("modrinth", [F("a1", "1.20.1", ["fabric"], pid="A", deps=[req("B", "vPIN")])], "1.20.1", "fabric")
        check("a dependency that names an exact file gets exactly that file", ids(res["added"]) == ["vPIN"], res)
        res = resolve.add_dependencies("modrinth", [F("a1", "1.20.1", ["fabric"], pid="A", deps=[req("B", "vGONE")])], "1.20.1", "fabric")
        check("a pinned file that cannot be found is skipped as not_found", res["added"] == [] and res["skipped"] == [{"project_id": "B", "reason": "not_found"}], res)
        res = resolve.add_dependencies("modrinth", [F("a1", "1.20.1", ["fabric"], pid="A", deps=[req("", "vPIN")])], "1.20.1", "fabric")
        check("a dependency known only by its version id works too", ids(res["added"]) == ["vPIN"], res)

    with PatchedSources(Adapter(bulk_error="offline")):
        res = resolve.add_dependencies("modrinth", start, "1.20.1", "fabric")
        check("a failed bulk call is a skipped dependency with the reason, the rest still returns",
              res["ok"] and res["added"] == [] and res["skipped"] == [{"project_id": "B", "reason": "offline"}], res)

    fab_only = {"B": [F("b1", "1.20.1", ["fabric"], pid="B")]}
    adapter = Adapter(files=fab_only)
    with PatchedSources(adapter):
        res = resolve.add_dependencies("modrinth", start, "1.20.1", "quilt")
        check("Quilt: a dependency with only a Fabric file is added, marked via fabric", ids(res["added"]) == ["b1"] and res["added"][0].get("via") == "fabric", res)
        check("...and was fetched with the loader filter off", adapter.list_calls[-1] == (["B"], "1.20.1", None), adapter.list_calls)

    with PatchedSources(None):
        check("no adapter: unsupported", resolve.add_dependencies("curseforge", start, "1.20.1", "fabric")["reason"] == "unsupported")


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
