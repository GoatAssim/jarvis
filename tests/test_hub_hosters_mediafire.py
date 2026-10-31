"""Download Hub, link-resolver plan phase 1: the MediaFire resolver, alone and through the hosters engine.

The pages are tests/fixtures/hosters/mediafire/*.html: SYNTHESISED from the plan's description (a downloadButton
anchor), not recorded from the real site.  The fallbacks the plan names (a download<digits>.mediafire.com address in
the text, a `khtmlurl` variable) are tested on literal strings, because the fake server can't be called
download123.mediafire.com.

Run: python tests/test_hub_hosters_mediafire.py
"""
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-mf-")
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
from jarvis.hub.hosters import mediafire as mf  # noqa: E402

passed = failed = 0
BASE, STATE = T.BASE, T.STATE
R = hosters.get("mediafire")
PAGE = "https://www.mediafire.com/file/abc/x.zip/file"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def test_matching():
    check("the page host is claimed, with and without www", hosters.find("https://mediafire.com/file/k/n/file").id == "mediafire"
          and hosters.find(PAGE).id == "mediafire")
    check("a download<digits> CDN address is NOT a page: it goes straight to the HTTP engine",
          hosters.find("https://download1234.mediafire.com/abc/key/x.zip") is None)
    check("a look-alike host is not claimed", hosters.find("https://mediafire.com.evil.example/file/k/n/file") is None)


def test_parsing():
    base = "https://www.mediafire.com/file/k/n/file"
    page = ("<a href='/x'>x</a><a class='input' aria-label='Download file' id='downloadButton' "
            "href='https://download42.mediafire.com/a/b/n.zip?x=1&amp;y=2'>Download</a>")
    check("the downloadButton anchor is read, attribute order and &amp; included",
          mf.find_download_link(page, base) == "https://download42.mediafire.com/a/b/n.zip?x=1&y=2")
    check("double quotes work too", mf.find_download_link('<a id="downloadButton" href="https://d.example/f">', base) == "https://d.example/f")
    check("a protocol-relative href gets https", mf.find_download_link("<a id='downloadButton' href='//d.example/f'>", base) == "https://d.example/f")
    check("a dead #-button is skipped for the aria-label one",
          mf.find_download_link("<a id='downloadButton' href='#'>a</a><a aria-label='Download file' href='https://d.example/g'>b</a>", base)
          == "https://d.example/g")
    check("javascript: and data: hrefs are never a file address",
          mf.find_download_link("<a id='downloadButton' href='javascript:void(0)'>a</a>", base) is None
          and mf.find_download_link("<a id='downloadButton' href='data:text/html,x'>a</a>", base) is None)
    check("fallback 1: a download<digits>.mediafire.com address in the text",
          mf.find_download_link("<script>go('https://download77.mediafire.com/q/r/s.rar')</script>", base)
          == "https://download77.mediafire.com/q/r/s.rar")
    check("fallback 2: a khtmlurl variable",
          mf.find_download_link("<script>var khtmlurl = \"https://dl.example/k/file.7z\";</script>", base) == "https://dl.example/k/file.7z")
    check("the button wins over a fallback", mf.find_download_link(
        "<script>var khtmlurl='https://dl.example/WRONG'</script><a id='downloadButton' href='https://dl.example/RIGHT'>", base)
        == "https://dl.example/RIGHT")
    check("nothing to find is None, not a guess", mf.find_download_link("<html>Welcome</html>", base) is None)
    check("page text is never executed: a script is only read as text",
          mf.find_download_link("<script>window.location='https://evil.example/x'</script>", base) is None)


def test_resolve():
    STATE.reset()
    got = R.resolve(BASE + "/file/good/report.zip/file#frag", T.rctx())
    check("the button's address comes back", got.url == BASE + "/mfdl/good/report.zip?e=1&s=2")
    check("the page request left the fragment off", STATE.reqs[0][0] == "/file/good/report.zip/file")
    check("the file request must carry the PAGE as Referer (and a browser User-Agent)",
          got.headers["Referer"] == BASE + "/file/good/report.zip/file" and "Mozilla" in got.headers["User-Agent"])
    got = R.resolve(BASE + "/file/scrambled/s.zip/file", T.rctx())
    check("a dead first button falls through to the working one", got.url == BASE + "/mfdl/scrambled/s.zip")
    exc = T.raises(hb.ResolveFailed, R.resolve, BASE + "/file/nobutton/x.zip/file", T.rctx())
    check("a page with no button says so, naming both causes", exc is not None and "removed" in exc.message and "layout" in exc.message)
    check("a 404 page is Dead", T.raises(hb.Dead, R.resolve, BASE + "/file/gone/x.zip/file", T.rctx()) is not None)
    exc = T.raises(hb.Unsupported, R.resolve, BASE + "/folder/abc/my-stuff", T.rctx())
    check("a folder link is refused by name BEFORE any request", exc is not None and "folder" in exc.message)
    STATE.reset()
    T.raises(hb.Unsupported, R.resolve, BASE + "/folder/abc", T.rctx())
    check("and nothing was fetched for it", STATE.reqs == [])


def test_engine_end_to_end():
    store, lib = T.env("mediafire")
    item = T.claim(store, BASE + "/file/good/report.zip/file")
    out = T.run(store, item)
    check("a MediaFire page link downloads through the engine", out.kind == Outcome.DONE)
    final = Path(out.dest_file)
    check("the bytes are the server's, named from the file URL", final.read_bytes() == srv.payload(220_000) and final.name == "report.zip")
    check("the file lands under the library root", lib in final.parents)
    mfdl = [h for p, h in STATE.reqs if p.startswith("/mfdl/")]
    check("the server saw the Referer it insists on (it answers 500 without)", mfdl and all(h.get("Referer", "").startswith(BASE + "/file/good/") for h in mfdl))
    check("the row keeps the page link", store.get_item(item["id"])["source_url"] == BASE + "/file/good/report.zip/file")

    out = T.run(store, T.claim(store, BASE + "/file/nobutton/x.zip/file"))
    check("a changed page fails for good, pointing at hoster_doctor", out.kind == Outcome.FAILED and not out.retryable and "hoster_doctor" in out.error)
    out = T.run(store, T.claim(store, BASE + "/folder/abc/stuff"))
    check("a folder fails for good with the plain reason", out.kind == Outcome.FAILED and not out.retryable and "folder" in out.error)


test_matching()
test_parsing()
test_resolve()
test_engine_end_to_end()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
