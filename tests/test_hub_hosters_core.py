"""Download Hub, link-resolver plan phase 0: the parts every file host shares.

Covers: the registry (discovery, one owner per host, a broken module skipped and reported); the route rule (priority 60,
direct CDN links and plain links stay with HTTP, a forced engine still wins, the owner's off switch and extra hosts);
the guarded fetcher (SSRF on every hop, no body read for a file, Cookie dropped on a host change); `trusted_headers`
(a resolver's Cookie reaches the server and is stored NOWHERE); every error a resolver can raise and the outcome it
becomes; group expansion; the resolved-link checks (private address, decrypt); filename and segment rules; the
settings (defaults, tiers, validators).

Run: python tests/test_hub_hosters_core.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-hcore-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_hoster_env as T  # noqa: E402
import _hub_testserver as srv  # noqa: E402
from jarvis.hub import hosters, netguard, router  # noqa: E402
from jarvis.hub import settings as hub_settings  # noqa: E402
from jarvis.hub.engines import http as http_engine  # noqa: E402
from jarvis.hub.engines.base import Outcome  # noqa: E402
from jarvis.hub.hosters import base as hb  # noqa: E402
from jarvis.hub.hosters.fetch import Fetcher  # noqa: E402

passed = failed = 0
BASE, STATE = T.BASE, T.STATE


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


class Fake(hb.Resolver):
    """A resolver whose behaviour a test sets: `behave` is a callable (url, ctx) -> Resolved | list, or raises."""
    id = "faketest"
    label = "FakeHost"
    hosts = ("fakehost.example",)
    method = "test"
    behave = None
    seen_urls = None

    def resolve(self, url, ctx):
        if self.seen_urls is not None:
            self.seen_urls.append(url)
        return self.behave(url, ctx)


FAKE = Fake()
hosters.load()
hosters.RESOLVERS["faketest"] = FAKE


def settings_with_fake():
    return {"hosters": {"enabled": {}, "extra_hosts": {}}}


def test_registry():
    check("the three phase-1 resolvers are installed and nothing failed to load",
          {"buzzheavier", "gdrive", "mediafire"} <= set(hosters.ids()) and hosters.LOAD_ERRORS == {})
    owners = {}
    for r in hosters.resolvers():
        if r.id == "faketest":
            continue
        for h in r.hosts:
            owners.setdefault(h, []).append(r.id)
    check("every default host has exactly one owner", all(len(v) == 1 for v in owners.values()))
    check("every resolver says what it is (label, method, honest confidence)",
          all(r.label and r.method and r.confidence in ("high", "medium", "low") for r in hosters.resolvers() if r.id != "faketest"))
    pkg = Path(hosters.__file__).parent / "zz_broken_resolver.py"
    try:
        pkg.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        hosters.load(force=True)
        check("a module that fails to import is skipped and reported, the others still load",
              "zz_broken_resolver" in hosters.LOAD_ERRORS and "mediafire" in hosters.ids())
    finally:
        pkg.unlink()
        hosters.load(force=True)
        hosters.RESOLVERS["faketest"] = FAKE
    check("and it is gone once the file is", "zz_broken_resolver" not in hosters.LOAD_ERRORS)
    check("find() never raises on junk", all(hosters.find(x) is None for x in (None, "", "not a url", "ftp://mediafire.com/x", "magnet:?xt=urn:btih:abc", "http://")))


def test_router():
    st = {"hosters": {}}
    d = router.route("https://www.mediafire.com/file/k/n/file", settings=st)
    check("a hoster page link routes to the hosters engine with a readable reason", d["engine"] == "hosters" and d["ok"]
          and d["reason"] == "MediaFire link (hosters engine)")
    check("a plain direct link still goes to HTTP", router.route("https://example.com/a.zip", settings=st)["engine"] == "http")
    check("a hoster's CDN address still goes to HTTP", router.route("https://download9.mediafire.com/a/b/c.zip", settings=st)["engine"] == "http")
    check("a YouTube link is untouched", router.route("https://www.youtube.com/watch?v=abcdefghijk", settings=st)["engine"] == "ytdlp")
    check("a magnet is untouched", router.route("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", settings=st)["engine"] != "hosters")
    check("forcing the http engine still works on a hoster link", router.route("https://www.mediafire.com/file/k/n/file", engine="http", settings=st)["engine"] == "http")
    off = {"hosters": {"enabled": {"mediafire": False}}}
    check("the owner's off switch sends the link back to HTTP", router.route("https://www.mediafire.com/file/k/n/file", settings=off)["engine"] == "http")
    extra = {"hosters": {"extra_hosts": {"mediafire": ["mf.example.org"]}}}
    check("a host the owner added is routed", router.route("https://mf.example.org/file/k/n/file", settings=extra)["engine"] == "hosters")
    check("routing works with no settings at all (everything on)", router.route("https://buzzheavier.com/abc123")["engine"] == "hosters")
    from jarvis.hub import engines
    by_name = {(r.engine, getattr(r.match, "__name__", "")): r.priority for r in engines.route_rules()}
    check("its priority is 60: above Stremio (50) and yt-dlp's any-site rule (40), below yt-dlp's YouTube rule (70)",
          by_name[("hosters", "_match_hoster")] == 60 and by_name[("stremio", "_match")] == 50
          and by_name[("ytdlp", "_match_site")] == 40 and by_name[("ytdlp", "_match_youtube")] == 70)
    check("and well below an owner's qBittorrent preference for magnets (500)", by_name[("qbittorrent", "_match_preferred")] > 60)


def test_fetcher():
    STATE.reset()
    f = Fetcher(allow_private_hosts=["127.0.0.1"])
    exc = T.raises(netguard.GuardError, f.get, BASE + "/redir-private")
    check("a redirect to a private address is refused at the second hop", exc is not None)
    page = f.get(BASE + "/redirect?size=10", follow=False)
    check("follow=False returns the 3xx itself", page.status == 302 and page.headers.get("Location"))
    page = f.get(BASE + "/file.bin?size=5000000")
    check("a file response is not read at all (only its headers)", not page.is_text and page.body == b"" and page.status == 200)
    page = f.get(BASE + "/echo-headers")
    check("a JSON answer is read and parsed", page.is_text and "User-Agent" in page.json())
    f2 = Fetcher(allow_private_hosts=["127.0.0.1"], user_agent="TestUA/1")
    check("the User-Agent is the owner's", f2.get(BASE + "/echo-headers").json()["User-Agent"] == "TestUA/1")
    check("a closed port is Transient (retryable) and the message has no query string",
          (T.raises(hb.Transient, Fetcher(allow_private_hosts=["127.0.0.1"], timeout=2).get, "http://127.0.0.1:9/x?token=SECRET") or hb.Transient("SECRET")).message.find("SECRET") < 0)
    STATE.reset()
    f3 = Fetcher(allow_private_hosts=["127.0.0.1", "localhost"])
    f3.get(BASE + "/redirect-host?size=10", headers={"Cookie": "sid=abc", "Authorization": "Bearer x", "X-Keep": "1"})
    first, second = STATE.headers[0], STATE.headers[1]
    check("a Cookie and Authorization are dropped when a redirect changes host, other headers stay",
          first.get("Cookie") == "sid=abc" and "Cookie" not in second and "Authorization" not in second and second.get("X-Keep") == "1")
    check("the private-address guard applies to the first request too", T.raises(netguard.GuardError, Fetcher().get, "http://10.0.0.1/x") is not None)


def test_trusted_headers():
    check("clean_trusted_headers allows a Cookie and the safe names", http_engine.clean_trusted_headers(
        {"Cookie": "sid=1", "referer": "https://x/", "User-Agent": "UA", "Evil": "x"}) == {"Cookie": "sid=1", "Referer": "https://x/", "User-Agent": "UA"})
    check("a value with a line break, NUL, or length over 4000 is dropped",
          http_engine.clean_trusted_headers({"Cookie": "a\r\nX: y", "Referer": "a\nb", "User-Agent": "a\x00b"}) == {}
          and http_engine.clean_trusted_headers({"Cookie": "x" * 4001}) == {} and http_engine.clean_trusted_headers("nope") == {})
    check("the OLD path still refuses a Cookie (only a resolver, in memory, may send one)",
          "Cookie" not in http_engine.clean_headers({"cookie": "x", "referer": "https://a/"}))

    FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/needcookie.bin?size=70000", headers={"Cookie": "sid=s3cret", "Referer": "https://page.example/p"})
    store, lib = T.env("faketest", {"hosters.extra_hosts": {"faketest": ["127.0.0.1"]}})
    item = T.claim(store, BASE + "/p", resolver="faketest")
    out = T.run(store, item)
    check("the server received the resolver's Cookie and Referer", out.kind == Outcome.DONE and
          any(h.get("Cookie") == "sid=s3cret" and h.get("Referer") == "https://page.example/p" for p, h in STATE.reqs if p.startswith("/needcookie")))
    dump = json.dumps({"row": store.get_item(item["id"]), "events": store.list_events(item["id"], 200)}, default=str)
    check("the Cookie is in NO row field, source_ref or event", "s3cret" not in dump)
    check("and the file is the server's", Path(out.dest_file).read_bytes() == srv.payload(70000))
    on_disk = "".join(p.read_bytes().decode("latin-1") for p in Path(HOME).rglob("hub-*.db*") if p.is_file())
    check("nor anywhere in the database files", "s3cret" not in on_disk)

    FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/needcookie.bin?size=70000", headers={"Referer": "https://page.example/p"})
    out = T.run(store, T.claim(store, BASE + "/p2", resolver="faketest"))
    check("without the Cookie the server's 403 becomes 'ask for a fresh link', not a dead end", out.kind == Outcome.FAILED and out.retryable)

    FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/redirect-host?size=10", headers={"Cookie": "sid=s3cret"})
    s2, _ = T.env("faketest", {"allow_private_hosts": ["127.0.0.1", "localhost"]})
    STATE.reset()
    out = T.run(s2, T.claim(s2, BASE + "/p3", resolver="faketest"))
    check("a redirect to another host does not carry the resolver's Cookie along", out.kind == Outcome.DONE and len(STATE.headers) >= 2
          and STATE.headers[0].get("Cookie") == "sid=s3cret" and "Cookie" not in STATE.headers[1])


def test_outcome_mapping():
    cases = [
        ("NeedsWait", hb.NeedsWait(75, "free users wait"), True, 75, "wait of"),
        ("QuotaExceeded", hb.QuotaExceeded(600, "daily limit"), True, 600, "daily limit"),
        ("Transient", hb.Transient("host answered 503"), True, None, "503"),
        ("NeedsHuman", hb.NeedsHuman("a captcha", open_url="https://fakehost.example/p?token=SECRET"), False, None, "Needs you"),
        ("Dead", hb.Dead("gone for good"), False, None, "gone for good"),
        ("Unsupported", hb.Unsupported("folders aren't supported"), False, None, "folders"),
        ("ResolveFailed", hb.ResolveFailed("no button"), False, None, "hoster_doctor"),
    ]
    store, _ = T.env("faketest")
    for name, exc, retryable, delay, text in cases:
        FAKE.behave = lambda url, ctx, e=exc: (_ for _ in ()).throw(e)
        out = T.run(store, T.claim(store, BASE + "/x-" + name, resolver="faketest"))
        check("%s -> failed, retryable=%s, delay=%s, says %r" % (name, retryable, delay, text),
              out.kind == Outcome.FAILED and out.retryable is retryable and out.delay_s == delay and text in out.error)
    FAKE.behave = lambda url, ctx: (_ for _ in ()).throw(hb.NeedsHuman("a captcha", open_url="https://fakehost.example/p?token=SECRET"))
    out = T.run(store, T.claim(store, BASE + "/nh", resolver="faketest"))
    check("a NeedsHuman message names the page but never its query string", "fakehost.example" in out.error and "SECRET" not in out.error)
    FAKE.behave = lambda url, ctx: (_ for _ in ()).throw(netguard.GuardError("refused: private address"))
    out = T.run(store, T.claim(store, BASE + "/g", resolver="faketest"))
    check("a guard refusal inside a resolver is a plain non-retryable failure", out.kind == Outcome.FAILED and not out.retryable and "refused" in out.error)
    FAKE.behave = lambda url, ctx: (_ for _ in ()).throw(ValueError("bug in a resolver"))
    out = T.run(store, T.claim(store, BASE + "/bug", resolver="faketest"))
    check("a bug in a resolver fails the row with a message; it never raises past the engine", out.kind == Outcome.FAILED and "ValueError" in out.error)
    FAKE.behave = lambda url, ctx: (_ for _ in ()).throw(hb.Stopped("cancel"))
    out = T.run(store, T.claim(store, BASE + "/s", resolver="faketest"))
    check("Stopped(cancel) from inside a resolver is a cancelled row", out.kind == Outcome.CANCELLED)
    FAKE.behave = lambda url, ctx: (_ for _ in ()).throw(hb.Stopped("shutdown"))
    check("Stopped(shutdown) is a requeue", T.run(store, T.claim(store, BASE + "/s2", resolver="faketest")).kind == Outcome.REQUEUE)


def test_resolved_checks():
    store, _ = T.env("faketest")
    FAKE.behave = lambda url, ctx: hb.Resolved("http://10.0.0.1/secret.bin")
    out = T.run(store, T.claim(store, BASE + "/priv", resolver="faketest"))
    check("a resolved link to a private address is refused, not fetched", out.kind == Outcome.FAILED and not out.retryable and "refused" in out.error)
    FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/file.bin?size=100", decrypt="mega")
    out = T.run(store, T.claim(store, BASE + "/enc", resolver="faketest"))
    check("a file that needs local decryption is refused, never saved as ciphertext", out.kind == Outcome.FAILED and "decryption" in out.error)
    FAKE.behave = lambda url, ctx: "not a Resolved"
    out = T.run(store, T.claim(store, BASE + "/junk", resolver="faketest"))
    check("a resolver that returns junk fails cleanly", out.kind == Outcome.FAILED)
    store2, _ = T.env()
    out = T.run(store2, T.claim(store2, "https://unowned.example/p"))
    check("a row whose resolver was switched off / is unknown fails with that reason", out.kind == Outcome.FAILED and "no file-host resolver" in out.error)


def test_filename_and_segments():
    store, lib = T.env("faketest")
    FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/file.bin?size=5000", filename="from-the-api.bin")
    first = T.claim(store, BASE + "/n1", resolver="faketest")
    out = T.run(store, first)
    check("a name the host SAID is used for the file", Path(out.dest_file).name == "from-the-api.bin")
    check("and for the row's title", store.get_item(first["id"])["title"] == "from-the-api.bin")
    out = T.run(store, T.claim(store, BASE + "/n2", resolver="faketest", filename="mine.bin"))
    check("the owner's filename option beats the host's", Path(out.dest_file).name == "mine.bin")
    FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/file.bin?size=5000", filename="../../evil.exe")
    out = T.run(store, T.claim(store, BASE + "/n3", resolver="faketest"))
    check("a hostile name from a host is made safe and stays under the library root", out.dest_file and lib in Path(out.dest_file).parents and ".." not in Path(out.dest_file).name)

    seen = []
    real = http_engine.stream_to_file
    http_engine.stream_to_file = lambda ctx, item, url, **kw: (seen.append(kw), real(ctx, item, url, **kw))[1]
    try:
        FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/file.bin?size=5000")
        store3, _ = T.env("faketest", {"http.segments": 6})
        T.run(store3, T.claim(store3, BASE + "/seg1", resolver="faketest"))
        check("with no cap of its own, the owner's segment count is passed on", seen[-1]["segments"] == 6)
        FAKE.max_segments = 1
        T.run(store3, T.claim(store3, BASE + "/seg2", resolver="faketest"))
        check("a single-use link (max_segments=1) is never split", seen[-1]["segments"] == 1)
        FAKE.max_segments = 3
        T.run(store3, T.claim(store3, BASE + "/seg3", resolver="faketest"))
        check("a host's own cap lowers the count", seen[-1]["segments"] == 3)
    finally:
        http_engine.stream_to_file = real
        FAKE.max_segments = None


def test_expansion():
    store, lib = T.env("faketest")
    kids = [hb.Resolved(BASE + "/x", filename="file%d.bin" % i, page_url="https://fakehost.example/c/%d" % i) for i in range(3)]
    FAKE.behave = lambda url, ctx: list(kids)
    parent = T.claim(store, "https://fakehost.example/folder/1", resolver="faketest", subdir="Mods", filename="ONE-NAME.bin")
    out = T.run(store, parent)
    rows = [r for r in store.list_items(owner="hub", limit=50) if r["id"] != parent["id"]]
    check("a list becomes one child row per file and the folder row finishes", out.kind == Outcome.DONE and len(rows) == 3)
    check("each child keeps its own page link and the resolver, in ONE group",
          {r["source_url"] for r in rows} == {"https://fakehost.example/c/%d" % i for i in range(3)}
          and all(r["options"].get("resolver") == "faketest" and r["engine"] == "hosters" for r in rows) and len({r["group_id"] for r in rows}) == 1
          and rows[0]["group_id"])
    check("children inherit the destination but NOT the parent's single file name", all(r["options"].get("subdir") == "Mods" and "filename" not in r["options"] for r in rows))
    out = T.run(store, T.claim(store, "https://fakehost.example/folder/1", resolver="faketest", subdir="Mods"))
    rows2 = [r for r in store.list_items(owner="hub", limit=100) if r["source_url"].startswith("https://fakehost.example/c/")]
    check("expanding the same folder again queues nothing twice", len(rows2) == 3)
    FAKE.behave = lambda url, ctx: [hb.Resolved(BASE + "/x", page_url="https://fakehost.example/many/%d" % i) for i in range(250)]
    out = T.run(store, T.claim(store, "https://fakehost.example/folder/2", resolver="faketest"))
    many = [r for r in store.list_items(owner="hub", limit=1000) if "/many/" in r["source_url"]]
    check("a huge folder is cut at the cap and says so", len(many) == 200)
    FAKE.behave = lambda url, ctx: []
    out = T.run(store, T.claim(store, "https://fakehost.example/folder/3", resolver="faketest"))
    check("an empty list is a clear failure, not a silent success", out.kind == Outcome.FAILED and "nothing to download" in out.error)
    FAKE.behave = lambda url, ctx: [hb.Resolved(BASE + "/x")]
    out = T.run(store, T.claim(store, "https://fakehost.example/folder/4", resolver="faketest"))
    check("a list entry with no page_url can't become a row (it would have nothing to re-resolve)", out.kind == Outcome.FAILED)
    # a child with a page link no host rule matches resolves through its own options.resolver
    FAKE.behave = lambda url, ctx: hb.Resolved(BASE + "/file.bin?size=4000")
    child = T.claim(store, "https://nobody-owns-this.invalid/c/9", resolver="faketest")
    check("a child whose page link no host rule matches still resolves through options.resolver", T.run(store, child).kind == Outcome.DONE)


def test_settings():
    d = hub_settings.DEFAULTS
    check("defaults: a hosters block, concurrency.hosters, no resolver switched off",
          d["concurrency"]["hosters"] == 3 and d["hosters"]["max_retries"] == 4 and d["hosters"]["enabled"] == {} and d["hosters"]["health"] == {})
    store, _ = T.env()
    got = store.get_settings()
    check("the store merges them in for a fresh install", got["hosters"]["max_retries"] == 4 and got["concurrency"]["hosters"] == 3)
    ok = hub_settings.validate_update
    P = hub_settings.PREFS_WRITABLE
    check("dl_prefs may switch a resolver and set its limits", ok({"hosters.enabled.mediafire": False, "concurrency.hosters": 2, "hosters.max_retries": 6}, P) == (
        {"hosters.enabled.mediafire": False, "concurrency.hosters": 2, "hosters.max_retries": 6}, []))
    check("an unknown resolver id is an error that names the known ones", "known:" in ok({"hosters.enabled.nope": True}, P)[1][0])
    check("the tier marker itself is not a setting", ok({"hosters.enabled.*": True}, P)[1] != [])
    check("a non-boolean switch is refused", ok({"hosters.enabled.mediafire": "maybe"}, P)[1] != [])
    check("ranges are enforced", ok({"concurrency.hosters": 0}, P)[1] != [] and ok({"hosters.max_retries": 11}, P)[1] != [])
    for key, val in (("hosters.user_agent", "UA"), ("hosters.extra_hosts", {"mediafire": ["a.example"]}), ("hosters.canary", {"gdrive": "https://x.example/f"})):
        check("%s needs dl_settings (it asks first), not dl_prefs" % key, ok({key: val}, P)[1] != [] and ok({key: val})[1] == [])
    check("extra_hosts refuses a scheme, a path, a space, a bad id", all(ok({"hosters.extra_hosts": v})[1] != [] for v in (
        {"mediafire": ["https://a.example"]}, {"mediafire": ["a.example/x"]}, {"mediafire": ["a b"]}, {"nope": ["a.example"]}, {"mediafire": ["x"] * 21}, ["a"])))
    check("extra_hosts lower-cases and de-duplicates", ok({"hosters.extra_hosts": {"mediafire": ["A.Example", "a.example."]}})[0] == {"hosters.extra_hosts": {"mediafire": ["a.example"]}})
    check("canary refuses a non-http link, credentials, spaces", all(ok({"hosters.canary": v})[1] != [] for v in (
        {"gdrive": "ftp://x.example/f"}, {"gdrive": "https://u:p@x.example/f"}, {"gdrive": "https://x.example/a b"}, {"gdrive": ""})))
    check("a user agent with a control character is refused", ok({"hosters.user_agent": "a\nb"})[1] != [])
    check("hosters.health is written by the hub only, never by a tool", ok({"hosters.health": {}})[1] != [] and ok({"hosters.health.mediafire": {}}, P)[1] != [])
    store.set_settings({"hosters.enabled.mediafire": False})
    check("a stored switch reads back through get_settings and is honoured", store.get_settings()["hosters"]["enabled"] == {"mediafire": False}
          and hosters.find("https://mediafire.com/file/k/n/file", store.get_settings()) is None)
    store.set_settings({"hosters.health.gdrive": {"at": "2026-10-30T10:00:00+01:00", "ok": True, "note": "ok"}})
    check("health recorded by the hub merges into the block", hosters.health(store.get_settings(), "gdrive")["ok"] is True and hosters.health(store.get_settings(), "mediafire") is None)


test_registry()
test_router()
test_fetcher()
test_trusted_headers()
test_outcome_mapping()
test_resolved_checks()
test_filename_and_segments()
test_expansion()
test_settings()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
