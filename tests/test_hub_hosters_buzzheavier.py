"""Download Hub, link-resolver plan phase 1: the Buzzheavier resolver, alone and through the hosters engine.

Runs against tests/_hub_fake_hosters.py, which behaves the way the PLAN describes the site (GET <page>/download with a
Referer, the file address in the hx-redirect header).  That proves the code does what the plan says; it cannot prove
the real site still does.  `hoster_doctor` is the check against the real thing.

Run: python tests/test_hub_hosters_buzzheavier.py
"""
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-bh-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_hoster_env as T  # noqa: E402
import _hub_testserver as srv  # noqa: E402
from jarvis.hub import hosters  # noqa: E402
from jarvis.hub.engines.base import Outcome  # noqa: E402
from jarvis.hub.hosters import base as hb  # noqa: E402

passed = failed = 0
BASE, STATE = T.BASE, T.STATE
R = hosters.get("buzzheavier")


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def test_matching():
    check("the resolver is registered with its five hosts", R is not None and set(R.hosts) == {
        "buzzheavier.com", "bzzhr.co", "bzzhr.to", "fuckingfast.co", "fuckingfast.net"})
    for host in R.hosts:
        check("%s/<id> is claimed (and www.%s too)" % (host, host),
              hosters.find("https://%s/abc123" % host).id == "buzzheavier"
              and hosters.find("https://www.%s/abc123" % host).id == "buzzheavier")
    check("a look-alike host is not claimed", hosters.find("https://notbuzzheavier.com/abc") is None
          and hosters.find("https://buzzheavier.com.evil.example/abc") is None)
    check("the owner can switch it off", hosters.find("https://buzzheavier.com/abc", {"hosters": {"enabled": {"buzzheavier": False}}}) is None)
    check("the owner can add a host that moved", hosters.find("https://newhome.example/abc", {"hosters": {"extra_hosts": {"buzzheavier": ["newhome.example"]}}}).id == "buzzheavier")


def test_resolve():
    STATE.reset()
    got = R.resolve(BASE + "/goodfile", T.rctx())
    check("a relative hx-redirect becomes a full file address", got.url.startswith(BASE + "/fdl/goodfile?t="))
    first_path, first_headers = STATE.reqs[0]
    check("it asked <page>/download", first_path == "/goodfile/download")
    check("with the page as Referer", first_headers.get("Referer") == BASE + "/goodfile")
    check("and without HX-Request the first time", "HX-Request" not in first_headers)
    check("the file request will carry the page as Referer and a browser User-Agent",
          got.headers.get("Referer") == BASE + "/goodfile" and "Mozilla" in got.headers.get("User-Agent", ""))
    check("a name it only guesses is not claimed (the file server names it)", got.filename is None)
    check("repr never shows the address's path or query, or a header value",
          "/fdl/" not in repr(got) and "?t=" not in repr(got) and "Mozilla" not in repr(got) and "127.0.0.1" in repr(got))

    for label, url in (("a query and fragment", BASE + "/goodfile?x=1#frag"), ("a trailing slash", BASE + "/goodfile/"),
                       ("a /download suffix", BASE + "/goodfile/download")):
        STATE.reset()
        R.resolve(url, T.rctx())
        check("%s normalise to the same request" % label,
              STATE.reqs[0][0] == "/goodfile/download" and STATE.reqs[0][1].get("Referer") == BASE + "/goodfile")

    got = R.resolve(BASE + "/absolute", T.rctx())
    check("an absolute hx-redirect is kept", got.url.startswith(BASE + "/fdl/absolute?t="))

    STATE.reset()
    got = R.resolve(BASE + "/needhx", T.rctx())
    check("a missing header is retried once with HX-Request: true", len(STATE.reqs) == 2
          and STATE.reqs[1][1].get("HX-Request") == "true" and "/fdl/needhx" in got.url)

    got = R.resolve(BASE + "/locmode", T.rctx())
    check("a plain 3xx Location is accepted as the file address", "/fdl/locmode" in got.url)


def test_failures():
    STATE.reset()
    exc = T.raises(hb.ResolveFailed, R.resolve, BASE + "/nohdr", T.rctx())
    check("no header even on the retry is ResolveFailed (the site changed), after exactly two requests",
          exc is not None and len(STATE.reqs) == 2 and "changed" in exc.message)
    check("a 404 is Dead", T.raises(hb.Dead, R.resolve, BASE + "/gone", T.rctx()) is not None)
    exc = T.raises(hb.QuotaExceeded, R.resolve, BASE + "/busy", T.rctx())
    check("a 429 is a quota with the host's own Retry-After", exc is not None and exc.retry_after_s == 120)
    check("a 5xx is Transient (worth a retry)", T.raises(hb.Transient, R.resolve, BASE + "/err500", T.rctx()) is not None)
    check("a folder-shaped address is refused by name, not guessed",
          T.raises(hb.Unsupported, R.resolve, BASE + "/f/abc/def", T.rctx()) is not None)
    check("the front page is refused by name", T.raises(hb.Unsupported, R.resolve, BASE + "/", T.rctx()) is not None)
    check("a pause asked for mid-resolve stops it", T.raises(hb.Stopped, R.resolve, BASE + "/goodfile", T.rctx(stop=lambda: "pause")) is not None)


def test_engine_end_to_end():
    store, lib = T.env("buzzheavier")
    item = T.claim(store, BASE + "/goodfile")
    out = T.run(store, item)
    row = store.get_item(item["id"])
    check("a Buzzheavier page link downloads through the engine", out.kind == Outcome.DONE and out.dest_file)
    final = Path(out.dest_file)
    check("the bytes are the server's own", final.read_bytes() == srv.payload(200_000))
    check("the file lands under the library root", lib in final.parents)
    fdl = [(p, h) for p, h in STATE.reqs if p.startswith("/fdl/")]
    check("the file request carried the page as Referer", fdl and all(h.get("Referer") == BASE + "/goodfile" for _, h in fdl))
    check("the row keeps the PAGE link as its source, not the signed file link",
          row["source_url"] == BASE + "/goodfile" and "/fdl/" not in str(row["source_url"]) and "t=" not in str(row))
    check("the log says which resolver found the link, and hides the query",
          any("Buzzheavier gave the file link" in e["message"] and "t=" not in e["message"]
              for e in store.list_events(item["id"])))


def test_expired_link_resolves_again():
    store, lib = T.env("buzzheavier")
    item = T.claim(store, BASE + "/flaky")
    first = T.run(store, item)
    check("a refused (403) fresh link is retryable, with a short delay, not a dead end",
          first.kind == Outcome.FAILED and first.retryable and first.delay_s == 3 and "fresh one" in first.error)
    second = T.run(store, store.get_item(item["id"]))
    asked = [p for p, _ in STATE.reqs if p == "/flaky/download"]
    check("the next attempt resolves AGAIN and then succeeds", len(asked) == 2 and second.kind == Outcome.DONE
          and Path(second.dest_file).read_bytes() == srv.payload(150_000))


def test_engine_failures():
    store, _ = T.env("buzzheavier")
    out = T.run(store, T.claim(store, BASE + "/gone"))
    check("a removed file fails for good, in words", out.kind == Outcome.FAILED and not out.retryable and "isn't there" in out.error)
    out = T.run(store, T.claim(store, BASE + "/busy"))
    check("a quota waits out the host's own time", out.kind == Outcome.FAILED and out.retryable and out.delay_s == 120)
    out = T.run(store, T.claim(store, BASE + "/err500"))
    check("a 5xx is retried with the normal backoff", out.kind == Outcome.FAILED and out.retryable and out.delay_s is None)
    out = T.run(store, T.claim(store, BASE + "/nohdr"))
    check("a changed site says so and points at hoster_doctor", out.kind == Outcome.FAILED and not out.retryable
          and "hoster_doctor" in out.error)
    STATE.reset()
    out = T.run(store, T.claim(store, BASE + "/goodfile"), stop=lambda: "pause")
    check("pause before the work starts pauses, fetching nothing", out.kind == Outcome.PAUSED and STATE.reqs == [])


test_matching()
test_resolve()
test_failures()
test_engine_end_to_end()
test_expired_link_resolves_again()
test_engine_failures()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
