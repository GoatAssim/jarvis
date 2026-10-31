"""Download Hub, link-resolver plan phase 1: the Google Drive resolver, alone and through the hosters engine.

The warning page is tests/fixtures/hosters/gdrive/warning.html: SYNTHESISED from the plan's description of the
`download-form` page, not recorded from Google.  The quota wording is a plain text match on the condition the plan
names, which is the weakest part of this resolver; hoster_doctor is the check against the real service.

Run: python tests/test_hub_hosters_gdrive.py
"""
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = tempfile.mkdtemp(prefix="jarvis-hub-gd-")
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
from jarvis.hub.hosters import gdrive  # noqa: E402

passed = failed = 0
BASE, STATE = T.BASE, T.STATE
R = hosters.get("gdrive")
gdrive.DOWNLOAD_URL = BASE + "/download"          # the real value is drive.usercontent.google.com/download
PAGE = "https://drive.google.com/file/d/%s/view?usp=sharing"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def test_ids():
    good = "1AbCdEfGhIjKlMn"
    shapes = {"file/d/<id>/view": "https://drive.google.com/file/d/%s/view?usp=sharing" % good,
              "file/u/0/d/<id>": "https://drive.google.com/file/u/0/d/%s/edit" % good,
              "open?id=": "https://drive.google.com/open?id=%s" % good,
              "uc?id=": "https://drive.google.com/uc?export=download&id=%s" % good,
              "usercontent download?id=": "https://drive.usercontent.google.com/download?id=%s&export=download" % good}
    for label, url in shapes.items():
        check("the id is found in %s" % label, gdrive.file_id_of(url) == good and hosters.find(url).id == "gdrive")
    check("a folder is refused by name", T.raises(hb.Unsupported, gdrive.file_id_of, "https://drive.google.com/drive/folders/%s" % good) is not None)
    check("a folder in a user profile path is refused too", T.raises(hb.Unsupported, gdrive.file_id_of, "https://drive.google.com/drive/u/0/folders/%s" % good) is not None)
    check("the Drive home page is refused, not saved as HTML", T.raises(hb.Unsupported, gdrive.file_id_of, "https://drive.google.com/") is not None)
    check("an id with odd characters is refused", T.raises(hb.Unsupported, gdrive.file_id_of, "https://drive.google.com/open?id=../../x") is not None)
    check("docs.google.com is not claimed", hosters.find("https://docs.google.com/document/d/%s/edit" % good) is None)


def test_parse_form():
    page = ('<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
            '<input type="hidden" name="id" value="A"><input type="hidden" name="export" value="download">'
            '<input type="hidden" name="uuid" value="u&amp;1"><input type="submit" value="go"></form>')
    got = gdrive.parse_confirm_form(page, "https://drive.usercontent.google.com/download?id=A")
    q = parse_qs(urlsplit(got).query)
    check("the form's hidden inputs become the GET, submit buttons left out",
          got.startswith("https://drive.usercontent.google.com/download?") and q == {"id": ["A"], "export": ["download"], "uuid": ["u&1"]})
    check("a form that points at ANOTHER host is not followed",
          gdrive.parse_confirm_form(page.replace("drive.usercontent.google.com/download", "evil.example/x"),
                                    "https://drive.usercontent.google.com/download") is None)
    check("a form that points at http (not https) is not followed",
          gdrive.parse_confirm_form(page.replace("https://drive", "http://drive"), "https://drive.usercontent.google.com/d") is None)
    check("no form is None", gdrive.parse_confirm_form("<html></html>", "https://x/") is None)


def test_resolve():
    STATE.reset()
    got = R.resolve(PAGE % "smallfileid1", T.rctx())
    check("a small file is answered at once; its address is the usercontent download", got.url.startswith(BASE + "/download?") and len(STATE.reqs) == 1)
    q = parse_qs(urlsplit(STATE.reqs[0][0]).query)
    check("the first request is id + export=download + authuser=0 + confirm=t",
          q == {"id": ["smallfileid1"], "export": ["download"], "authuser": ["0"], "confirm": ["t"]})
    check("the name and size come from the answer's own headers", got.filename == "gdrive file.bin" and got.size == 150_000)

    STATE.reset()
    got = R.resolve(PAGE % "bigfileid001", T.rctx())
    q = parse_qs(urlsplit(got.url).query)
    check("a big file's warning page is recognised (by content type) and its form is submitted as a GET",
          len(STATE.reqs) == 2 and q.get("uuid") == ["u-123"] and q.get("confirm") == ["t"] and q.get("id") == ["bigfileid001"])
    check("the RFC 5987 name is decoded", got.filename == "big archive.bin")

    STATE.reset()
    exc = T.raises(hb.ResolveFailed, R.resolve, PAGE % "loopformid01", T.rctx())
    check("a warning page that comes back after the form is answered once, then given up on (no loop)",
          exc is not None and len(STATE.reqs) == 2)
    exc = T.raises(hb.QuotaExceeded, R.resolve, PAGE % "quotaid0001", T.rctx())
    check("'too many users' (an HTTP 200 page!) is a quota, retried much later", exc is not None and exc.retry_after_s >= 3600)
    check("a 403 page says it isn't shared 'anyone with the link'", "anyone with the link" in (T.raises(hb.Dead, R.resolve, PAGE % "forbidden001", T.rctx()) or hb.Dead("")).message)
    check("a 404 page is Dead", T.raises(hb.Dead, R.resolve, PAGE % "missing0001", T.rctx()) is not None)
    exc = T.raises(hb.ResolveFailed, R.resolve, PAGE % "surprise0001", T.rctx())
    check("a page nobody expects is 'page may have changed', not a crash", exc is not None and "changed" in exc.message)
    check("a pause stops it between its two requests", T.raises(hb.Stopped, R.resolve, PAGE % "bigfileid001", T.rctx(stop=lambda: "cancel")) is not None)


def test_engine_end_to_end():
    store, lib = T.env("gdrive")
    out = T.run(store, T.claim(store, PAGE % "smallfileid1"))
    final = Path(out.dest_file) if out.dest_file else None
    check("a small Drive file downloads through the engine, under its real name",
          out.kind == Outcome.DONE and final.name == "gdrive file.bin" and final.read_bytes() == srv.payload(150_000))
    check("the probe fetched headers only, then the engine fetched the file (no body read twice by the resolver)",
          [p.split("?")[0] for p, _ in STATE.reqs] == ["/download", "/download"])

    store, lib = T.env("gdrive")
    out = T.run(store, T.claim(store, PAGE % "bigfileid001"))
    final = Path(out.dest_file) if out.dest_file else None
    check("a big file goes through the confirm form and downloads", out.kind == Outcome.DONE and final.name == "big archive.bin"
          and final.read_bytes() == srv.payload(700_000))

    out = T.run(store, T.claim(store, PAGE % "quotaid0001"))
    check("a quota waits (retryable, an hour), in the host's terms", out.kind == Outcome.FAILED and out.retryable and out.delay_s >= 3600
          and "too many people" in out.error)
    out = T.run(store, T.claim(store, PAGE % "missing0001"))
    check("a missing file fails for good", out.kind == Outcome.FAILED and not out.retryable)
    out = T.run(store, T.claim(store, "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMn"))
    check("a folder fails for good with the plain reason", out.kind == Outcome.FAILED and not out.retryable and "folders" in out.error)


test_ids()
test_parse_form()
test_resolve()
test_engine_end_to_end()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
