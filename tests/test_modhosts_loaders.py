"""jarvis/modhosts/loaders.py: the "latest loader version" resolver (owner answer 8, agent P5b).

Covers:
  * table-driven answers per loader against tests/data/modhosts/hand_loader_*.json (Fabric newest stable, Quilt
    newest without a pre-release tag, Forge recommended else latest, NeoForge newest stable of the Minecraft
    version with the 1.A.B and yy.drop.patch prefix rules); no list order is trusted (scrambled fixtures, numbers
    that sort wrongly as text, a newer beta that must not win);
  * the failure modes: `offline`, `vendor_changed`, `no_loader_version`, `unsupported`; a failure is never cached and
    never falls back to another loader's site (the Forge maven list is read only when the promotions file answers
    and names neither key);
  * the 10-minute on-disk cache (hit, expiry, aliases share a key, failures not cached, a poisoned cache file
    ignored, only the small derived answer stored, never a key);
  * hostile answers (wrong types, huge strings, HTML, markup in versions, junk lists, a body over the cap);
  * allow-list enforcement through net.py (a removed host sends nothing, a redirect off the list is refused, the
    module opens no connection and reads no host from config or the environment);
  * Forge and NeoForge are different loaders; valid_loader_version, mrpack_dependency_key, curseforge_loader_id.

No network and no real `requests`: tests/_modhosts_fake_loader_meta.py is a transport behind net.py.
Run: python tests/test_modhosts_loaders.py
"""
import json
import os
import re
import sys
import tempfile
import time
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-modhosts-loaders-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_CHANNEL_SENDER", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from _modhosts_fake_loader_meta import (ALL_HOSTS, FABRIC_HOST, FORGE_HOST, FORGE_MAVEN_HOST,  # noqa: E402
                                        NEOFORGE_HOST, NEOFORGE_LEGACY_PATH, NEOFORGE_PATH, QUILT_HOST,
                                        FakeLoaderMeta)
from jarvis.modhosts import cache, hosts, loaders, net  # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-0123456789abcdef"          # obviously fake: no real key is ever used in a test
CONFIG_NAME = "jarvis.modhosts.config"
LOADERS = ("fabric", "quilt", "forge", "neoforge")
SITE = {"fabric": FABRIC_HOST, "quilt": QUILT_HOST, "forge": FORGE_HOST, "neoforge": NEOFORGE_HOST}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


# ------------------------------------------------------------------ helpers

class Fresh:
    """A new fake loader site installed behind net.py, with an empty on-disk cache; restored afterwards."""

    def __enter__(self):
        cache.clear()
        self.fake = FakeLoaderMeta().install()
        return self.fake

    def __exit__(self, *exc):
        self.fake.uninstall()


class Config:
    """Stands in for jarvis.modhosts.config with a key configured (the loader sites must never see it)."""

    def __enter__(self):
        self.had = CONFIG_NAME in sys.modules
        self.old = sys.modules.get(CONFIG_NAME)
        module = types.ModuleType(CONFIG_NAME)
        module.get_key = lambda: FAKE_KEY
        sys.modules[CONFIG_NAME] = module
        return self

    def __exit__(self, *exc):
        if self.had:
            sys.modules[CONFIG_NAME] = self.old
        else:
            sys.modules.pop(CONFIG_NAME, None)


def latest(loader, mc):
    return loaders.latest_loader_version(loader, mc)


def json_reply(body, status=200):
    return net.Reply(status, {"content-type": "application/json"}, json.dumps(body))


def html_reply(status=200):
    return net.Reply(status, {"content-type": "text/html"}, "<html><body><h1>Down for maintenance</h1></body></html>")


def is_ok(res, version, how):
    return (isinstance(res, dict) and res.get("ok") is True and res.get("loader_version") == version
            and res.get("how") == how and set(res) <= {"ok", "loader_version", "how", "note"})


def is_fail(res, reason):
    return (isinstance(res, dict) and res.get("ok") is False and res.get("reason") == reason
            and isinstance(res.get("error"), str) and 0 < len(res["error"]) < 400)


def sentence_is_clean(res):
    text = res.get("error", "")
    return "://" not in text and FAKE_KEY not in text and "boom" not in text and "<" not in text


# ------------------------------------------------------------------ the small public helpers

def test_valid_loader_version():
    good = ["0.20.1", "26.3.0.99", "21.11.48", "0.20.0-beta.9", "26.1.1.15-beta", "10.13.4.1614-1.7.10", "a", "1",
            "A" * 40, "fabric-0.16.9"]
    bad = ["", " ", "1 2", "1.2 ", " 1.2", "\n1.2", "1.2\n", "a" * 41, "-1", "1-", "1..2", ".1", "1.", "1/2", "1\\2",
           "../x", "1;rm", "<b>1</b>", "1.2\u00e9", "1\x00", "1\t2", "1+2", "1_2", "'1'", '"1"', None, 123, 1.5,
           b"1.2", ["1"], {"v": "1"}, True]
    for text in good:
        check("valid: %r" % text, loaders.valid_loader_version(text) is True)
    for text in bad:
        check("invalid: %r" % (text,), loaders.valid_loader_version(text) is False)


def test_dependency_keys_and_curseforge_ids():
    keys = {"fabric": "fabric-loader", "Fabric": "fabric-loader", " quilt ": "quilt-loader",
            "quilt-loader": "quilt-loader", "forge": "forge", "Forge": "forge", "neoforge": "neoforge",
            "Neo Forge": "neoforge", "neoforged": "neoforge"}
    for loader, want in keys.items():
        check("mrpack key for %r" % loader, loaders.mrpack_dependency_key(loader) == want,
              loaders.mrpack_dependency_key(loader))
    for loader in ("liteloader", "", None, 5, "fabric;", ["fabric"]):
        check("no mrpack key for %r" % (loader,), loaders.mrpack_dependency_key(loader) is None)
    ids = [("fabric", "0.16.9", "fabric-0.16.9"), ("Quilt", "0.26.4", "quilt-0.26.4"),
           ("forge", "52.1.20", "forge-52.1.20"), ("neoforge", "21.1.219", "neoforge-21.1.219"),
           ("forge", "21.1.219", "forge-21.1.219")]
    for loader, version, want in ids:
        check("curseforge id %s %s" % (loader, version), loaders.curseforge_loader_id(loader, version) == want,
              loaders.curseforge_loader_id(loader, version))
    check("Forge and NeoForge ids are different",
          loaders.curseforge_loader_id("forge", "1.2.3") != loaders.curseforge_loader_id("neoforge", "1.2.3"))
    for loader, version in (("liteloader", "1.0"), ("fabric", None), ("fabric", ""), ("fabric", "0.1 2"),
                            ("fabric", "x" * 41), (None, "1.0"), ("fabric", "<b>")):
        check("no curseforge id for %r %r" % (loader, version),
              loaders.curseforge_loader_id(loader, version) is None)


def test_neoforge_prefix_rules():
    table = {"1.21.11": "21.11.", "1.21.10": "21.10.", "1.21.1": "21.1.", "1.21": "21.0.", "1.20.4": "20.4.",
             "1.20.1": "20.1.", "26.3": "26.3.0.", "26.2": "26.2.0.", "26.1": "26.1.0.", "26.1.1": "26.1.1.",
             "26.1.2": "26.1.2."}
    for mc, want in table.items():
        check("neoforge prefix of %s" % mc, loaders.neoforge_prefix(mc) == want, loaders.neoforge_prefix(mc))
    for mc in ("x", "1", "1.2.3.4", "", "1.x"):
        check("no neoforge prefix for %r" % mc, loaders.neoforge_prefix(mc) is None)


# ------------------------------------------------------------------ Fabric and Quilt

def test_fabric_table():
    table = [
        ("26.3", "0.20.1"),            # the probe's real answer: the only entry flagged stable
        ("1.21.11", "0.17.2"),         # scrambled order, a newer pre-release (0.18.0-beta.1) and 0.17.3 not stable
        ("Minecraft 1.21.11", "0.17.2"),
        ("v26.3", "0.20.1"),
    ]
    for mc, want in table:
        with Fresh() as fake:
            res = latest("fabric", mc)
            check("fabric %s -> %s" % (mc, want), is_ok(res, want, "stable"), res)
            check("fabric: one request, to the Fabric site only",
                  len(fake.calls) == 1 and fake.calls[0]["host"] == FABRIC_HOST, fake.calls)
    with Fresh() as fake:
        latest("fabric", "26.3")
        call = fake.calls[0]
        check("fabric: the exact fixed path", call["path"] == "/v2/versions/loader/26.3", call["path"])
        check("fabric: a long per-request timeout (the answer takes about 20 s)", call["timeout"] >= 60, call["timeout"])
        agent = call["headers"].get("User-Agent", "")
        check("fabric: the honest User-Agent", agent.startswith("Jarvis/") and "github.com/GoatAssim" in agent, agent)
        check("fabric: no key header of any kind", not any(k.lower() in ("x-api-key", "authorization", "cookie")
                                                           for k in call["headers"]), call["headers"])


def test_fabric_no_build():
    for mc, label in (("1.16.5", "only pre-releases"), ("1.8", "an empty list"), ("1.2", "an unknown version (400 [])")):
        with Fresh() as fake:
            res = latest("fabric", mc)
            check("fabric %s: %s -> no_loader_version" % (mc, label), is_fail(res, "no_loader_version"), res)
            check("fabric %s: the sentence names the Minecraft version" % mc, mc in res.get("error", ""), res)
            check("fabric %s: nothing but the Fabric site was asked" % mc, set(fake.hosts_called()) == {FABRIC_HOST})
    with Fresh() as fake:
        fake.overrides[FABRIC_HOST] = json_reply({"error": "boom"}, status=400)
        res = latest("fabric", "26.3")
        check("fabric: a 400 that is not the known empty list is vendor_changed, not 'no build'",
              is_fail(res, "vendor_changed"), res)


def test_quilt_table():
    with Fresh() as fake:
        res = latest("quilt", "26.3")
        check("quilt 26.3 -> 0.26.4 (newest by number, not the first entry: the list opens with 0.20.0 betas)",
              is_ok(res, "0.26.4", "stable"), res)
        check("quilt: one request, to the Quilt site only", len(fake.calls) == 1 and fake.calls[0]["host"] == QUILT_HOST)
        check("quilt: the exact fixed path", fake.calls[0]["path"] == "/v3/versions/loader/26.3", fake.calls[0]["path"])
    for tag_version, label in (("0.27.0-rc.1", "rc"), ("0.25.0-pre.2", "pre"), ("0.26.0-alpha.3", "alpha"),
                               ("0.28.0-beta.1", "beta")):
        check("quilt: %s is a pre-release" % label, loaders._has_pre_tag(tag_version) is True)
    for plain in ("0.24.0", "0.26.4", "1.0.0", "0.9.3"):
        check("quilt: %s is not a pre-release" % plain, loaders._has_pre_tag(plain) is False)
    with Fresh() as fake:
        res = latest("quilt", "1.16.5")
        check("quilt 1.16.5: only pre-releases -> no_loader_version", is_fail(res, "no_loader_version"), res)
    with Fresh() as fake:
        res = latest("quilt", "1.2")
        check("quilt: an unknown Minecraft version (404 not_found JSON) -> no_loader_version",
              is_fail(res, "no_loader_version"), res)
    with Fresh() as fake:
        fake.overrides[QUILT_HOST] = html_reply(404)
        res = latest("quilt", "26.3")
        check("quilt: a 404 that is not the known JSON (the endpoint moved) is vendor_changed",
              is_fail(res, "vendor_changed"), res)
    with Fresh() as fake:
        fake.overrides[QUILT_HOST] = json_reply({"code": "something_else"}, status=404)
        res = latest("quilt", "26.3")
        check("quilt: a 404 JSON with another code is vendor_changed", is_fail(res, "vendor_changed"), res)


# ------------------------------------------------------------------ Forge

def test_forge_table():
    table = [
        ("26.3", "66.1.0", "recommended"), ("26.2", "65.1.5", "recommended"), ("26.1.2", "64.1.5", "recommended"),
        ("1.21.11", "61.2.4", "recommended"), ("1.21.10", "60.1.18", "recommended"),
        ("1.21.1", "52.1.20", "recommended"),
        ("26.1.1", "63.0.2", "latest"), ("26.1", "62.0.9", "latest"), ("1.21.9", "59.0.5", "latest"),
        ("1.7.10", "10.13.4.1614-1.7.10", "recommended"),
    ]
    for mc, want, how in table:
        with Fresh() as fake:
            res = latest("forge", mc)
            check("forge %s -> %s (%s)" % (mc, want, how), is_ok(res, want, how) or
                  (res.get("ok") and res.get("loader_version") == want and res.get("how") == how and
                   isinstance(res.get("note"), str)), res)
            if how == "latest":
                check("forge %s: a note says there is no recommended build" % mc,
                      "recommended" in res.get("note", "") and want in res.get("note", ""), res)
            else:
                check("forge %s: no note for a recommended build" % mc, "note" not in res, res)
            check("forge %s: only the promotions file was read" % mc,
                  fake.hosts_called() == [FORGE_HOST], fake.calls)
    with Fresh() as fake:
        latest("forge", "26.3")
        check("forge: the exact fixed path", fake.calls[0]["path"] == "/net/minecraftforge/forge/promotions_slim.json")
    with Fresh() as fake:
        res = latest("forge", "26.3")
        check("forge 26.3 reads only its own keys (the 26.30 decoy says 99.0.0)", res.get("loader_version") == "66.1.0", res)


def test_forge_maven_fallback():
    for mc, want in (("1.21.3", "53.1.0"), ("1.21", "51.0.33")):
        with Fresh() as fake:
            res = latest("forge", mc)
            check("forge %s: no promotion at all -> newest build of Forge's own list (%s)" % (mc, want),
                  res.get("ok") is True and res.get("loader_version") == want and res.get("how") == "latest", res)
            check("forge %s: says where it came from" % mc, "version list" in res.get("note", ""), res)
            check("forge %s: promotions first, then Forge's own maven list, nothing else" % mc,
                  fake.hosts_called() == [FORGE_HOST, FORGE_MAVEN_HOST], fake.calls)
    with Fresh() as fake:
        res = latest("forge", "9.9")
        check("forge 9.9: in neither list -> no_loader_version", is_fail(res, "no_loader_version"), res)
        check("forge 9.9: both Forge lists were asked, nothing else", fake.hosts_called() == [FORGE_HOST, FORGE_MAVEN_HOST])
    with Fresh() as fake:
        fake.overrides[FORGE_MAVEN_HOST] = net.Reply(500, {}, b"oops")
        res = latest("forge", "1.21.3")
        check("forge: the maven list down -> offline", is_fail(res, "offline"), res)
    with Fresh() as fake:
        fake.overrides[FORGE_MAVEN_HOST] = html_reply()
        res = latest("forge", "1.21.3")
        check("forge: a maven answer with no versions in it -> vendor_changed", is_fail(res, "vendor_changed"), res)
    with Fresh() as fake:
        fake.overrides[FORGE_HOST] = net.Reply(500, {}, b"oops")
        res = latest("forge", "1.21.3")
        check("forge: a FAILED promotions request is offline and never triggers the maven list",
              is_fail(res, "offline") and fake.hosts_called() == [FORGE_HOST], (res, fake.calls))
    with Fresh() as fake:
        fake.overrides[FORGE_HOST] = html_reply()
        res = latest("forge", "1.21.3")
        check("forge: an unreadable promotions answer is vendor_changed and never triggers the maven list",
              is_fail(res, "vendor_changed") and fake.hosts_called() == [FORGE_HOST], (res, fake.calls))
    with Fresh() as fake:
        fake.overrides[FORGE_HOST] = json_reply({"promos": {"26.3-recommended": None, "26.3-latest": "66.1.0"}})
        res = latest("forge", "26.3")
        check("forge: a null recommended means none: the latest is used and said so",
              res.get("ok") and res.get("how") == "latest" and res.get("loader_version") == "66.1.0", res)


# ------------------------------------------------------------------ NeoForge

def test_neoforge_table():
    table = [
        ("26.3", "26.3.0.99", "stable"),        # the list is not sorted and holds a newer beta (26.3.0.100-beta)
        ("26.2", "26.2.0.93", "stable"),
        ("26.1.2", "26.1.2.124", "stable"),     # 124 beats the text-sorted 99, and the newer 130-beta
        ("1.21.11", "21.11.48", "stable"),      # 48 beats the text-sorted 9
        ("1.21.10", "21.10.64", "stable"),
        ("1.21.1", "21.1.219", "stable"),       # the 21.11.x builds must not match the 21.1. prefix
        ("1.20.2", "20.2.86", "stable"),
        ("26.1.1", "26.1.1.15-beta", "latest"),  # only betas: the newest beta, not a failure
        ("26.1", "26.1.0.19-beta", "latest"),    # and the 26.1.1.x / 26.1.2.x builds are not 26.1
        ("1.21.9", "21.9.16-beta", "latest"),
    ]
    for mc, want, how in table:
        with Fresh() as fake:
            res = latest("neoforge", mc)
            good = res.get("ok") is True and res.get("loader_version") == want and res.get("how") == how
            check("neoforge %s -> %s (%s)" % (mc, want, how), good, res)
            if how == "latest":
                check("neoforge %s: a note says only betas exist" % mc,
                      "beta" in res.get("note", "") and want in res.get("note", ""), res)
            else:
                check("neoforge %s: no note for a stable build" % mc, "note" not in res, res)
            check("neoforge %s: one request, to the NeoForge site only" % mc,
                  len(fake.calls) == 1 and fake.calls[0]["host"] == NEOFORGE_HOST and fake.calls[0]["path"] == NEOFORGE_PATH,
                  fake.calls)
    for mc in ("26.9", "1.19.4", "1.20"):
        with Fresh() as fake:
            res = latest("neoforge", mc)
            check("neoforge %s: no build -> no_loader_version" % mc, is_fail(res, "no_loader_version"), res)
    with Fresh() as fake:
        res = latest("neoforge", "1.20.1")
        check("neoforge 1.20.1: the legacy artifact is not read -> no_loader_version", is_fail(res, "no_loader_version"), res)
        check("neoforge 1.20.1: the legacy path was never asked for", NEOFORGE_LEGACY_PATH not in fake.paths_called(),
              fake.paths_called())
        check("neoforge 1.20.1: the sentence tells the owner to name a version", "loader version" in res.get("error", ""))


# ------------------------------------------------------------------ strictness

def test_forge_and_neoforge_are_different_loaders():
    with Fresh() as fake:
        forge = latest("forge", "1.21.1")
        neo = latest("neoforge", "1.21.1")
        check("1.21.1: Forge and NeoForge answer with their own, different versions",
              forge.get("loader_version") == "52.1.20" and neo.get("loader_version") == "21.1.219", (forge, neo))
        check("each asked only its own site", fake.hosts_called() == [FORGE_HOST, NEOFORGE_HOST], fake.calls)
    with Fresh() as fake:
        res = latest("neoforge", "1.20.1")
        check("NeoForge does not borrow Forge's 1.20.1 build", res.get("ok") is False and FORGE_HOST not in fake.hosts_called())
    for down, others in (("neoforge", ("forge", "fabric", "quilt")), ("forge", ("neoforge", "fabric", "quilt")),
                         ("fabric", ("quilt", "forge", "neoforge")), ("quilt", ("fabric", "forge", "neoforge"))):
        with Fresh() as fake:
            fake.overrides[SITE[down]] = net.Reply(503, {}, b"down")
            res = latest(down, "26.3")
            check("%s down -> offline" % down, is_fail(res, "offline"), res)
            check("%s down: no other loader's site was tried" % down,
                  set(fake.hosts_called()) == {SITE[down]}, fake.calls)
    with Fresh() as fake:
        for spelling, loader in (("NeoForge", "neoforge"), ("neo forge", "neoforge"), ("quilt-loader", "quilt"),
                                 ("Fabric", "fabric"), ("minecraftforge", "forge")):
            fake.calls.clear()
            res = latest(spelling, "26.3")
            check("spelling %r goes to the %s site" % (spelling, loader),
                  res.get("ok") is True and fake.calls and fake.calls[0]["host"] == SITE[loader], (res, fake.calls))


# ------------------------------------------------------------------ failure modes

def test_unusable_input_sends_nothing():
    for loader in ("liteloader", "", None, 5, "fabric; rm -rf", "x" * 5000, ["fabric"], "vanilla"):
        with Fresh() as fake:
            res = latest(loader, "26.3")
            check("loader %r -> unsupported" % (str(loader)[:20],), is_fail(res, "unsupported"), res)
            check("...nothing sent", fake.calls == [])
            check("...a short sentence", len(res.get("error", "")) < 200, len(res.get("error", "")))
    for mc in (None, "", "latest", "1.21.x", "24w14a", "1.21.11-rc1", "1", "1.2.3.4", "../../etc", 26.3, "1.21.11 beta"):
        with Fresh() as fake:
            res = latest("fabric", mc)
            check("Minecraft version %r -> unsupported" % (mc,), is_fail(res, "unsupported"), res)
            check("...nothing sent", fake.calls == [])


def test_offline_for_every_loader():
    cases = [("a refused connection", ConnectionError("boom http://secret.example/x")),
             ("a timeout", TimeoutError("boom timed out")),
             ("HTTP 500", net.Reply(500, {}, b"oops")),
             ("HTTP 503", net.Reply(503, {}, b"oops"))]
    for loader in LOADERS:
        for label, reply in cases:
            with Fresh() as fake:
                fake.overrides[SITE[loader]] = reply
                res = latest(loader, "26.3")
                check("%s: %s -> offline" % (loader, label), is_fail(res, "offline"), res)
                check("%s: %s: the sentence has no URL, no exception text" % (loader, label), sentence_is_clean(res), res)
    for loader in LOADERS:
        with Fresh() as fake:
            fake.overrides[SITE[loader]] = net.Reply(429, {"Retry-After": "2"}, b"")
            res = latest(loader, "26.3")
            check("%s: rate limited -> offline, with the wait in the sentence" % loader,
                  is_fail(res, "offline") and "Wait about" in res["error"], res)
            check("%s: rate limited: one short retry and then stop, never a loop" % loader, len(fake.calls) == 2, len(fake.calls))


def test_vendor_changed_for_every_loader():
    for loader in LOADERS:
        shapes = [
            ("an HTML page", html_reply()),
            ("a JSON string", json_reply("nope")),
            ("a JSON number", json_reply(42)),
            ("a JSON object of the wrong kind", json_reply({"x": 1})),
            ("HTTP 403", json_reply({"message": "forbidden"}, status=403)),
            ("HTTP 418", net.Reply(418, {}, b"teapot")),
            ("an empty body", net.Reply(200, {"content-type": "application/json"}, b"")),
            ("a body over the size cap", net.Reply(200, {"content-type": "application/json"}, b" " * (17 * 1024 * 1024))),
        ]
        for label, reply in shapes:
            with Fresh() as fake:
                fake.overrides[SITE[loader]] = reply
                res = latest(loader, "26.3")
                check("%s: %s -> vendor_changed" % (loader, label), is_fail(res, "vendor_changed"), res)
                check("%s: %s: the sentence is clean" % (loader, label), sentence_is_clean(res), res)
    with Fresh() as fake:
        fake.overrides[FORGE_HOST] = net.Reply(404, {}, b"")
        res = latest("forge", "26.3")
        check("forge: the promotions file gone (404) is vendor_changed", is_fail(res, "vendor_changed"), res)
    with Fresh() as fake:
        fake.overrides[NEOFORGE_HOST] = json_reply({"isSnapshot": False, "versions": []})
        res = latest("neoforge", "26.3")
        check("neoforge: an empty version list is vendor_changed (the real one has thousands)",
              is_fail(res, "vendor_changed"), res)


def test_never_raises():
    old = net.fetch

    def boom(*args, **kwargs):
        raise RuntimeError("boom http://secret.example/%s" % FAKE_KEY)

    try:
        net.fetch = boom
        with Fresh():
            net.fetch = boom
            for loader in LOADERS:
                res = latest(loader, "26.3")
                check("%s: an exception inside net.fetch becomes a failure" % loader, res.get("ok") is False, res)
                check("%s: ...whose sentence leaks nothing" % loader,
                      "boom" not in res.get("error", "") and FAKE_KEY not in res.get("error", "")
                      and "://" not in res.get("error", ""), res)
    finally:
        net.fetch = old
    old_get = cache.get
    try:
        with Fresh():
            def bad_get(key):
                raise OSError("boom")
            cache.get = bad_get
            res = latest("fabric", "26.3")
            check("a cache that raises does not break the lookup", isinstance(res, dict) and "ok" in res, res)
    finally:
        cache.get = old_get


# ------------------------------------------------------------------ hostile answers

def fabric_entry(version, stable=True):
    return {"loader": {"version": version, "stable": stable, "build": 1}}


def test_hostile_fabric_and_quilt():
    junk = [{"loader": "x"}, {"loader": {"version": 5, "stable": True}}, {"loader": {"version": None, "stable": True}},
            {"loader": {"version": ["0.1"], "stable": True}}, "str", None, 7, [], [[]], {"loader": {}}]
    for loader in ("fabric", "quilt"):
        with Fresh() as fake:
            fake.overrides[SITE[loader]] = json_reply(junk)
            res = latest(loader, "26.3")
            check("%s: a list of junk entries -> vendor_changed" % loader, is_fail(res, "vendor_changed"), res)
    hostile_versions = ["<img src=x onerror=alert(1)>", "9" * 100000, "0.99 0", "0.99\n", "../../x", "1;rm -rf /",
                        "<script>", "0.50.0\u202e", "0.60.0" + "\x00"]
    for loader in ("fabric", "quilt"):
        entries = [fabric_entry(v) for v in hostile_versions] + [fabric_entry("0.12.3"), fabric_entry("0.12.2")]
        with Fresh() as fake:
            fake.overrides[SITE[loader]] = json_reply(entries)
            res = latest(loader, "26.3")
            check("%s: hostile versions are skipped, the valid one is chosen" % loader,
                  res.get("ok") is True and res.get("loader_version") == "0.12.3", res)
            check("%s: whatever comes back passes valid_loader_version" % loader,
                  loaders.valid_loader_version(res.get("loader_version")), res)
    with Fresh() as fake:
        fake.overrides[FABRIC_HOST] = json_reply([fabric_entry("0.30.0", "true"), fabric_entry("0.29.0", 1),
                                                  fabric_entry("0.28.0", None), fabric_entry("0.27.0", "yes")])
        res = latest("fabric", "26.3")
        check("fabric: only a literal JSON true counts as stable", is_fail(res, "no_loader_version"), res)
    with Fresh() as fake:
        fake.overrides[FABRIC_HOST] = json_reply({"loader": {"version": "0.1.0", "stable": True}})
        res = latest("fabric", "26.3")
        check("fabric: a single object instead of a list is vendor_changed", is_fail(res, "vendor_changed"), res)


def test_hostile_forge():
    bad_promos = [
        {"26.3-recommended": 66, "26.3-latest": ["x"]},
        {"26.3-recommended": "x" * 5000, "26.3-latest": "1.0"},
        {"26.3-recommended": "<script>alert(1)</script>"},
        {"26.3-recommended": {"v": "1.0"}},
        {"26.3-latest": "66.1.0 ; rm"},
        {"other": "1.0"},
        {},
    ]
    for promos in bad_promos:
        with Fresh() as fake:
            fake.overrides[FORGE_HOST] = json_reply({"promos": promos})
            res = latest("forge", "26.3")
            check("forge: hostile promos %s -> vendor_changed" % json.dumps(promos)[:50], is_fail(res, "vendor_changed"), res)
            check("forge: ...and the maven list was not read as a way around it", fake.hosts_called() == [FORGE_HOST])
    for body in (["a"], "promos", {"promos": ["a"]}, {"promos": "x"}, {"promos": None}, None):
        with Fresh() as fake:
            fake.overrides[FORGE_HOST] = json_reply(body)
            res = latest("forge", "26.3")
            check("forge: promos of the wrong type %s -> vendor_changed" % json.dumps(body)[:30],
                  is_fail(res, "vendor_changed"), res)
    # a hostile maven list: huge strings, markup, an entity bomb, thousands of rows
    bomb = ("<?xml version=\"1.0\"?><!DOCTYPE lolz [<!ENTITY a \"aaaaaaaaaa\"><!ENTITY b \"&a;&a;&a;&a;&a;&a;&a;\">]>"
            "<metadata><versioning><versions>"
            + "<version>1.21.3-<b>x</b></version><version>1.21.3-%s</version><version>1.21.3-&b;</version>" % ("9" * 5000)
            + "<version>1.21.3-53.0.1</version>"
            + "".join("<version>1.99-%d.0.0</version>" % i for i in range(3000))
            + "</versions></versioning></metadata>")
    with Fresh() as fake:
        fake.overrides[FORGE_MAVEN_HOST] = net.Reply(200, {"content-type": "text/xml"}, bomb)
        res = latest("forge", "1.21.3")
        check("forge: a hostile maven list yields only the clean build",
              res.get("ok") is True and res.get("loader_version") == "53.0.1", res)
    with Fresh() as fake:
        fake.overrides[FORGE_MAVEN_HOST] = net.Reply(200, {"content-type": "text/xml"}, "<version>" * 100000)
        res = latest("forge", "1.21.3")
        check("forge: a maven list that is a wall of tags does not hang or raise", res.get("ok") is False, res)


def test_hostile_neoforge():
    junk = [1, None, {"a": 1}, "x" * 5000, "<b>", "", " 26.3.0.5 ", "26.3.0.99\n", ["26.3.0.1"], True, 26.3]
    with Fresh() as fake:
        fake.overrides[NEOFORGE_HOST] = json_reply({"isSnapshot": False, "versions": junk})
        res = latest("neoforge", "26.3")
        check("neoforge: a list of nothing but junk -> vendor_changed", is_fail(res, "vendor_changed"), res)
    with Fresh() as fake:
        fake.overrides[NEOFORGE_HOST] = json_reply({"isSnapshot": False, "versions": ["<script>", "26.3.0.99\n"] + junk
                                                    + ["26.3.0.5", "26.3.0." + "9" * 100000]})
        res = latest("neoforge", "26.3")
        check("neoforge: junk is skipped, the one clean build wins", is_ok(res, "26.3.0.5", "stable"), res)
    for body in ([], "versions", {"versions": "26.3.0.1"}, {"versions": {"a": 1}}, {"versions": None}, {}):
        with Fresh() as fake:
            fake.overrides[NEOFORGE_HOST] = json_reply(body)
            res = latest("neoforge", "26.3")
            check("neoforge: versions of the wrong type %s -> vendor_changed" % json.dumps(body)[:30],
                  is_fail(res, "vendor_changed"), res)
    started = time.monotonic()
    with Fresh() as fake:
        fake.overrides[NEOFORGE_HOST] = json_reply({"versions": ["x"] * 250000 + ["26.3.0.1"]})
        res = latest("neoforge", "26.3")
        check("neoforge: a quarter of a million junk rows is cut off, not a hang", isinstance(res, dict) and res.get("ok") is False, res)
    check("...and quickly", time.monotonic() - started < 60, time.monotonic() - started)


def test_a_redirect_never_yields_another_sites_version():
    with Fresh() as fake:
        fake.overrides[FABRIC_HOST] = net.Reply(302, {"Location": "https://evil.example/x"}, b"")
        res = latest("fabric", "26.3")
        check("a redirect off the allow-list is refused (reported as offline)", is_fail(res, "offline"), res)
        check("...and the other host was never asked", fake.hosts_called() == [FABRIC_HOST], fake.calls)
    with Fresh() as fake:
        fake.overrides[FABRIC_HOST] = net.Reply(302, {"Location": "https://meta.quiltmc.org/v3/versions/loader/26.3"}, b"")
        res = latest("fabric", "26.3")
        check("fabric redirected to the Quilt site: Quilt's answer carries no stable flag, so no version comes out",
              res.get("ok") is False, res)
    with Fresh() as fake:
        fake.overrides[FORGE_HOST] = net.Reply(302, {"Location": "http://files.minecraftforge.net/elsewhere"}, b"")
        fake.sequence[FORGE_HOST] = [fake.overrides[FORGE_HOST], json_reply({"promos": {"26.3-recommended": "66.1.0"}})]
        res = latest("forge", "26.3")
        check("a redirect to plain http on an allowed host is upgraded to https and followed",
              is_ok(res, "66.1.0", "recommended"), res)
        check("...the second request went to https", all(c["url"].startswith("https://") for c in fake.calls), fake.calls)


# ------------------------------------------------------------------ cache

def _cache_doc():
    return json.loads(cache.cache_path().read_text(encoding="utf-8"))


def test_cache_hit_expiry_and_keys():
    with Fresh() as fake:
        first = latest("fabric", "26.3")
        calls = len(fake.calls)
        second = latest("fabric", "26.3")
        check("second identical lookup is a cache hit: same answer, no new request",
              first == second and len(fake.calls) == calls == 1, (first, second, len(fake.calls)))
        latest("Fabric", "v26.3")
        check("another spelling of the same loader and version shares the entry", len(fake.calls) == 1, len(fake.calls))
        latest("fabric", "1.21.11")
        check("another Minecraft version is another entry", len(fake.calls) == 2, len(fake.calls))
        latest("quilt", "26.3")
        check("another loader is another entry", len(fake.calls) == 3, len(fake.calls))
        entries = _cache_doc()["entries"]
        mine = {k: v for k, v in entries.items() if k.startswith(loaders.CACHE_PREFIX)}
        check("the entries are named loader_meta:v1:<loader>:<mc>",
              set(mine) == {"loader_meta:v1:fabric:26.3", "loader_meta:v1:fabric:1.21.11", "loader_meta:v1:quilt:26.3"}, set(mine))
        ttl = mine["loader_meta:v1:fabric:26.3"]["t"] - time.time()
        check("the time to live is about 10 minutes", 590 <= ttl <= 601, ttl)
        text = cache.cache_path().read_text(encoding="utf-8")
        check("only the small derived answer is stored, never the vendor's list",
              "launcherMeta" not in text and "intermediary" not in text and len(text) < 2500, len(text))
        check("the stored value is just version and how",
              mine["loader_meta:v1:fabric:26.3"]["v"] == {"loader_version": "0.20.1", "how": "stable"}, mine)
    real_now = cache._now
    try:
        with Fresh() as fake:
            latest("neoforge", "26.3")
            check("neoforge: one request", len(fake.calls) == 1)
            cache._now = lambda: time.time() + 601
            latest("neoforge", "26.3")
            check("after 10 minutes the entry has expired and the site is asked again", len(fake.calls) == 2, len(fake.calls))
    finally:
        cache._now = real_now
    with Fresh() as fake:
        latest("neoforge", "26.1.1")
        latest("neoforge", "26.1.1")
        check("a 'latest' answer keeps its note through the cache",
              len(fake.calls) == 1 and "beta" in latest("neoforge", "26.1.1").get("note", ""), fake.calls)


def test_failures_are_not_cached():
    with Fresh() as fake:
        fake.overrides[FABRIC_HOST] = net.Reply(500, {}, b"oops")
        check("offline first", is_fail(latest("fabric", "26.3"), "offline"))
        del fake.overrides[FABRIC_HOST]
        res = latest("fabric", "26.3")
        check("the next call asks again and succeeds (an offline answer was not cached)",
              is_ok(res, "0.20.1", "stable") and len(fake.calls) == 2, (res, len(fake.calls)))
    with Fresh() as fake:
        check("no build first", is_fail(latest("fabric", "1.8"), "no_loader_version"))
        fake.fabric["by_mc"]["1.8"] = [fabric_entry("0.7.7", True)]
        res = latest("fabric", "1.8")
        check("a version that gains a build is found at once (no_loader_version was not cached)",
              is_ok(res, "0.7.7", "stable"), res)
    with Fresh() as fake:
        fake.overrides[FORGE_HOST] = html_reply()
        latest("forge", "26.3")
        del fake.overrides[FORGE_HOST]
        check("vendor_changed was not cached either", is_ok(latest("forge", "26.3"), "66.1.0", "recommended"))


def test_poisoned_cache_is_ignored():
    key = loaders.CACHE_PREFIX + "fabric:26.3"
    poisons = [
        {"loader_version": "<script>alert(1)</script>", "how": "stable"},
        {"loader_version": "0.1.0", "how": "bogus"},
        {"loader_version": "0.1.0", "how": "stable", "note": "x" * 1000},
        {"loader_version": "0.1.0", "how": "stable", "note": ["x"]},
        {"loader_version": 5, "how": "stable"},
        {"how": "stable"},
        "0.1.0",
        ["0.1.0", "stable"],
    ]
    for poison in poisons:
        with Fresh() as fake:
            cache.put(key, poison, 600)
            res = latest("fabric", "26.3")
            check("poisoned cache value %s is ignored and the site is asked" % json.dumps(poison)[:40],
                  is_ok(res, "0.20.1", "stable") and len(fake.calls) == 1, (res, len(fake.calls)))
            check("...and the entry is repaired", cache.get(key) == {"loader_version": "0.20.1", "how": "stable"}, cache.get(key))
    with Fresh() as fake:
        cache.put(key, {"loader_version": "9.9.9", "how": "stable"}, 600)
        res = latest("fabric", "26.3")
        check("a well-formed cached answer is served without any request", is_ok(res, "9.9.9", "stable") and not fake.calls, res)
    with Fresh() as fake:
        cache.cache_path().write_text("{ not json", encoding="utf-8")
        res = latest("fabric", "26.3")
        check("a corrupt cache file is a miss, not an error", is_ok(res, "0.20.1", "stable"), res)


def test_a_configured_key_never_reaches_the_loader_sites_or_the_cache():
    with Config(), Fresh() as fake:
        for loader in LOADERS:
            latest(loader, "26.3")
        check("all four loaders were asked", set(fake.hosts_called()) == set(SITE.values()), fake.hosts_called())
        seen = [k.lower() for c in fake.calls for k in c["headers"]]
        check("no request carried a key, authorization or cookie header",
              not any(k in ("x-api-key", "authorization", "cookie", "proxy-authorization") for k in seen), seen)
        check("the key is in no header value", not any(FAKE_KEY in str(v) for c in fake.calls for v in c["headers"].values()))
        check("the key is not in the cache file", FAKE_KEY not in cache.cache_path().read_text(encoding="utf-8"))


# ------------------------------------------------------------------ allow-list and source rules

def test_every_address_is_fixed_and_on_the_allow_list():
    urls = [loaders.FABRIC_URL % "1.21.11", loaders.QUILT_URL % "1.21.11", loaders.FORGE_PROMOS_URL,
            loaders.FORGE_MAVEN_URL, loaders.NEOFORGE_URL]
    seen = set()
    for url in urls:
        host = re.match(r"https://([^/]+)/", url)
        check("https and a listed host: %s" % url, bool(host) and host.group(1) in hosts.LOADER_META_HOSTS, url)
        if host:
            seen.add(host.group(1))
    check("every allow-listed loader host is used by the resolver (no dead permission)",
          seen == set(hosts.LOADER_META_HOSTS), (seen, hosts.LOADER_META_HOSTS))
    check("the fake serves exactly those five hosts", set(ALL_HOSTS) == set(hosts.LOADER_META_HOSTS))
    check("net.allowed_hosts('loader_meta') is that block", net.allowed_hosts("loader_meta") == frozenset(hosts.LOADER_META_HOSTS))


def test_the_module_opens_no_connection_and_reads_no_host_from_outside():
    text = Path(loaders.__file__).read_text(encoding="utf-8")
    for needle in ("environ", "getenv", "import requests", "urllib", "socket", "http.client", "subprocess", "open(",
                   "api.curseforge.com", "x-api-key", "get_key", "tool_settings", "os.path"):
        check("loaders.py does not contain %r" % needle, needle not in text)
    code = text.split('"""', 2)[2]                                   # everything after the module docstring
    check("every request goes through ONE net.fetch call with the loader_meta vendor",
          code.count("net.fetch(") == 1 and 'net.fetch("loader_meta"' in code, code.count("net.fetch("))
    os.environ["LOADER_META_URL"] = "https://evil.example/x"
    os.environ["FABRIC_META_URL"] = "https://evil.example/x"
    try:
        with Fresh() as fake:
            latest("fabric", "26.3")
            check("a host named in the environment changes nothing", fake.hosts_called() == [FABRIC_HOST], fake.calls)
    finally:
        os.environ.pop("LOADER_META_URL", None)
        os.environ.pop("FABRIC_META_URL", None)


def test_a_host_removed_from_the_allow_list_sends_nothing():
    old = hosts.LOADER_META_HOSTS
    try:
        hosts.LOADER_META_HOSTS = tuple(h for h in old if h != FABRIC_HOST)
        with Fresh() as fake:
            res = latest("fabric", "26.3")
            check("fabric with its host off the list is refused (offline)", is_fail(res, "offline"), res)
            check("...and nothing was sent", fake.calls == [], fake.calls)
            other = latest("quilt", "26.3")
            check("the other loaders still work", other.get("ok") is True, other)
    finally:
        hosts.LOADER_META_HOSTS = old


def test_no_private_address_is_reached():
    with Fresh() as fake:
        net.RESOLVER = lambda host, port: ["10.0.0.5"]
        res = latest("fabric", "26.3")
        check("a loader host that resolves to a private address is refused (offline)", is_fail(res, "offline"), res)
        check("...and nothing was sent", fake.calls == [], fake.calls)


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
