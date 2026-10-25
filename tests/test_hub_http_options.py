"""Download Hub, brief 7C: the `filename` and `referer` options of dl_add_http.

Covers: the allow-list checkers (what `filename` and `referer` accept and each refusal, in the hub's words,
nothing silently fixed); both options are for the http engine only; dl_add_http (one link for a filename, a
dry run refuses a bad value, no `headers` argument); a stored `filename` reaching the engine (it names the
file, beats a Content-Disposition, still goes through unique_path and the name reservation); a stored
`referer` going out as the Referer on the first request and on nothing else the owner didn't ask for; no
Authorization / Cookie ever stored; and the Referer being kept on a host change exactly like today (an
existing test pins that; see the report's Open points).

Run: python tests/test_hub_http_options.py   (Linux, a local fake server, no network)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-httpopts-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.actions import download_add_http as dah  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import service  # noqa: E402
from jarvis.hub.engines import http as http_engine  # noqa: E402
from jarvis.hub.engines import stremio, ytdlp  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
PUBLIC = "http://93.184.216.34"
YT = "https://youtu.be/abcdefghijk"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


ytdlp.ENGINE.available = lambda: (True, "")
stremio.ENGINE.available = lambda: (True, "")


def rows():
    """What dl_list shows; `prefs` is a fixed set of keys, so filename / referer are read with opts() instead."""
    return {i["id"]: i for i in dh.tool_dl_list({})["items"]}


def opts(item_id):
    """The options STORED on a row (what the engine reads)."""
    return (service._store(None).get_item(item_id) or {}).get("options") or {}


# ---------------------------------------------------------------- filename: the checker
fn = service._check_filename
for good in ("report.pdf", "my file (2).iso", "a", "x" * 120, "Ünïcode ñame.txt", "no-extension", "v1.2.3.tar.gz"):
    out, problem = fn(good)
    check("filename %r is accepted as it is" % good[:30], out == good and problem is None)

refusals = [
    ("a path with /", "dir/file.bin", "path"),
    ("a path with a backslash", "dir\\file.bin", "path"),
    ("a parent folder", "../evil.exe", "path"),
    ("'..' inside a name", "a..b.bin", ".."),
    ("a drive letter", "C:file.bin", "drive"),
    ("a drive letter and a path", "C:\\x.bin", "path"),
    ("a NUL", "a\x00b.bin", "control"),
    ("a newline", "a\nb.bin", "control"),
    ("a tab", "a\tb.bin", "control"),
    ("a reserved name", "CON", "reserved"),
    ("a reserved name with an extension", "nul.txt", "reserved"),
    ("a reserved name, lower case", "com1", "reserved"),
    ("a leading dot", ".hidden", "dot"),
    ("a trailing dot", "file.", "dot"),
    ("a leading space", " file.bin", "space"),
    ("a trailing space", "file.bin ", "space"),
    ("an empty name", "", "empty"),
    ("a blank name", "   ", "empty"),
    ("a character Windows can't keep (:)", "ab:c.bin", "can't carry"),
    ("a character Windows can't keep (*)", "a*b.bin", "can't carry"),
    ("a character Windows can't keep (?)", "a?b.bin", "can't carry"),
    ("a character Windows can't keep (|)", "a|b.bin", "can't carry"),
    ("121 characters (the engine would cut it)", "x" * 121, "120"),
    ("200 characters (inside the 200 limit, still too long to keep)", "x" * 200, "120"),
    ("201 characters", "x" * 201, "200"),
    ("a number", 5, "text"),
    ("a bool", True, "text"),
    ("a list", ["a.bin"], "text"),
    ("None", None, "text"),
]
for label, bad, word in refusals:
    out, problem = fn(bad)
    check("filename refuses %s, in words (%r)" % (label, word), out is None and isinstance(problem, str) and word in problem)

out, _ = fn("e\u0301.txt")           # e + combining accent: the same letter as one character
check("filename normalises the same letters (NFC) rather than refusing them", out == "\u00e9.txt")

# ---------------------------------------------------------------- referer: the checker
rf = service._check_referer
for good in ("https://example.com/page", "http://example.com", "https://example.com/a?b=1&c=2", "https://example.com:8443/x#frag",
             "HTTPS://Example.com/", "https://" + "a" * 40 + ".com/" + "p" * 400):
    out, problem = rf(good)
    check("referer %r is accepted" % good[:36], out == good and problem is None and len(good) <= 500)
out, _ = rf("  https://example.com/x  ")
check("surrounding spaces are trimmed (they are not part of a link)", out == "https://example.com/x")

for label, bad, word in (
        ("a link over 500 characters", "https://example.com/" + "p" * 481, "500"),
        ("ftp", "ftp://example.com/x", "http"),
        ("javascript", "javascript:alert(1)", "http"),
        ("a file link", "file:///etc/passwd", "http"),
        ("no scheme", "example.com/page", "http"),
        ("no host", "https:///page", "http"),
        ("a space inside", "https://example.com/a b", "spaces"),
        ("a newline inside (header injection)", "https://example.com/a\r\nX-Evil: 1", "control"),
        ("a user name and password", "https://user:secret@example.com/", "password"),
        ("a user name only", "https://user@example.com/", "password"),
        ("a bad port", "https://example.com:99999/", "usable"),
        ("an empty string", "", "empty"),
        ("a number", 5, "http"),
        ("a bool", True, "http"),
        ("a dict", {"a": 1}, "http")):
    out, problem = rf(bad)
    check("referer refuses %s" % label, out is None and isinstance(problem, str) and word in problem)
check("a refused referer never echoes the secret back", "secret" not in (rf("https://user:secret@example.com/")[1] or ""))

# ---------------------------------------------------------------- the allow-list
norm = service.normalize_engine_item_options
check("both options pass the allow-list together with segments",
      norm({"segments": 3, "filename": "a.iso", "referer": "https://e.com/"})[0]
      == {"segments": 3, "filename": "a.iso", "referer": "https://e.com/"})
check("filename None / referer None are 'not given'", norm({"filename": None, "referer": None}) == ({}, None))
out, problem = norm({"filename": "../x"})
check("a bad filename in the allow-list is an error that names the option", out == {} and "filename" in problem)
out, problem = norm({"headers": {"Authorization": "Bearer x"}})
check("headers are STILL not an item option (the error names the known ones, headers is not among them)",
      out == {} and "headers" in problem and "filename" in problem and "referer" in problem and "segments" in problem)
out, problem = norm({"authorization": "Bearer x"})
check("neither is an Authorization option", out == {} and bool(problem))
out, problem = norm({"cookie": "a=b"})
check("nor a Cookie option", out == {} and bool(problem))

# ---------------------------------------------------------------- service.add_items
r = service.add_items([PUBLIC + "/a.iso"], start_paused=True, item_options={"filename": "mine.iso", "referer": "https://example.com/p"})
row = rows()[r["added"][0]["id"]]
check("add_items stores filename and referer on an http row",
      r["ok"] and row["engine"] == "http" and opts(row["id"]).get("filename") == "mine.iso" and opts(row["id"]).get("referer") == "https://example.com/p")
check("...and nothing in the answer says they were ignored", "ignored" not in r.get("note", ""))
stored = json.dumps(opts(row["id"]))
check("the stored options hold no Authorization / Cookie / headers key", not any(w in stored.lower() for w in ("authorization", "cookie", "headers")))

r = service.add_items([YT, PUBLIC + "/b.iso"], start_paused=True, item_options={"filename": "x.iso", "referer": "https://example.com/p"})
by_engine = {a["engine"]: rows()[a["id"]] for a in r["added"]}
check("a video row ignores filename and referer, the file row keeps them",
      "filename" not in opts(by_engine["ytdlp"]["id"]) and "referer" not in opts(by_engine["ytdlp"]["id"])
      and opts(by_engine["http"]["id"]).get("filename") == "x.iso")
check("...and the note says the other links ignored them", "ignored" in r.get("note", "") and "http" in r["note"])

n = len(rows())
r = service.add_items([PUBLIC + "/c.iso"], start_paused=True, item_options={"filename": "a/b.iso"})
check("a bad filename queues nothing", r["ok"] is False and "filename" in r["error"] and len(rows()) == n)
r = service.add_items([PUBLIC + "/c.iso"], start_paused=True, item_options={"referer": "https://u:p@example.com/"})
check("a referer with a password queues nothing", r["ok"] is False and "referer" in r["error"] and len(rows()) == n)

# ---------------------------------------------------------------- dl_add_http
sch = next(s for s in dah.TOOL_SCHEMAS if s["name"] == "dl_add_http")
props = sch["parameters"]["properties"]
check("the schema has filename and referer as text", props["filename"]["type"] == "string" and props["referer"]["type"] == "string")
check("the schema has no headers / authorization / cookie argument",
      not any(k in props for k in ("headers", "header", "authorization", "cookie", "cookies")))
check("the schema stays small", len(json.dumps(sch)) <= 2200)

r = dah.tool_dl_add_http({"url": PUBLIC + "/d.iso", "filename": "report.pdf", "start_paused": True})
row = rows()[r["added"][0]["id"]]
check("dl_add_http queues one link with filename", r["ok"] and row["engine"] == "http" and opts(row["id"]).get("filename") == "report.pdf")
r = dah.tool_dl_add_http({"url": PUBLIC + "/e.iso", "referer": "https://example.com/page", "start_paused": True})
check("dl_add_http queues with a referer", r["ok"] and opts(r["added"][0]["id"]).get("referer") == "https://example.com/page")
r = dah.tool_dl_add_http({"url": PUBLIC + "/f.iso", "filename": "both.iso", "referer": "https://example.com/p", "segments": 2, "start_paused": True})
p = opts(r["added"][0]["id"])
check("all three options go together", r["ok"] and p.get("filename") == "both.iso" and p.get("referer") == "https://example.com/p" and p.get("segments") == 2)

n = len(rows())
r = dah.tool_dl_add_http({"urls": [PUBLIC + "/g1.iso", PUBLIC + "/g2.iso"], "filename": "same.iso", "start_paused": True})
check("filename with several links is refused and nothing is queued",
      r.get("ok") is False and "one link" in r["error"] and len(rows()) == n)
r = dah.tool_dl_add_http({"urls": [PUBLIC + "/g1.iso", PUBLIC + "/g1.iso"], "filename": "same.iso", "start_paused": True})
check("...even when the two links are the same link", r.get("ok") is False and len(rows()) == n)
r = dah.tool_dl_add_http({"urls": [PUBLIC + "/g3.iso", PUBLIC + "/g4.iso"], "referer": "https://example.com/p", "start_paused": True})
check("a referer WITH several links is fine (it is not a per-file name)",
      r["ok"] and r["count"] == 2 and all(opts(a["id"]).get("referer") == "https://example.com/p" for a in r["added"]))

n = len(rows())
for label, args in (("a path", {"filename": "../x.iso"}), ("a long name", {"filename": "y" * 150}), ("a drive letter", {"filename": "D:x.iso"}),
                    ("a reserved name", {"filename": "aux"}), ("a number", {"filename": 5}), ("a list", {"filename": ["a"]}),
                    ("a bad referer", {"referer": "not a link"}), ("a referer with a password", {"referer": "https://a:b@example.com/"}),
                    ("a referer that is a number", {"referer": 7})):
    r = dah.tool_dl_add_http(dict({"url": PUBLIC + "/h.iso", "start_paused": True}, **args))
    check("%s is refused with words and nothing is queued" % label, r.get("ok") is False and bool(r.get("error")) and len(rows()) == n)
    r = dah.tool_dl_add_http(dict({"url": PUBLIC + "/h.iso", "dry_run": True}, **args))
    check("...the same on a dry run (%s)" % label, r.get("ok") is False and bool(r.get("error")) and len(rows()) == n)

r = dah.tool_dl_add_http({"url": PUBLIC + "/i.iso", "filename": "", "referer": "", "start_paused": True})
check("an empty filename / referer means 'not given'", r["ok"] and not {"filename", "referer"} & set(opts(r["added"][0]["id"])))
n = len(rows())
r = dah.tool_dl_add_http({"url": PUBLIC + "/j.iso", "filename": "dry.iso", "referer": "https://example.com/p", "dry_run": True})
check("dry_run echoes the options and queues nothing",
      r["ok"] and r["dry_run"] and r.get("item_options") == {"filename": "dry.iso", "referer": "https://example.com/p"} and len(rows()) == n)
r = dah.tool_dl_add_http({"url": YT, "filename": "x.mp4"})
check("a video page is still refused (it belongs to dl_add_ytdlp)", r.get("ok") is False and r.get("skipped"))
r = dah.tool_dl_add_http({"url": PUBLIC + "/k.iso", "headers": {"Authorization": "Bearer SECRET"}, "start_paused": True})
stored = json.dumps(opts(r["added"][0]["id"])) if r.get("ok") else ""
check("an unknown `headers` argument stores nothing (the secret is nowhere in the row)", "SECRET" not in stored and "Authorization" not in stored)
check("no Authorization / Cookie value is anywhere in what dl_list shows",
      "SECRET" not in json.dumps(dh.tool_dl_list({})))

# ---------------------------------------------------------------- the engine: filename and referer reach the request and the file
SERVER, BASE, STATE = srv.start()
_n = [0]


def env():
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hubx-{_n[0]}.db")
    store.set_settings({"library_root": str(lib), "allow_private_hosts": ["127.0.0.1", "localhost"], "http.stall_seconds": 5})
    STATE.reset()
    return store, lib


def claim(store, url, **options):
    item = store.add_item(engine="http", source_url=url, title="", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item):
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    return http_engine.ENGINE.run(item, ctx)


store, lib = env()
item = claim(store, BASE + "/file.bin?size=1000", filename="mine.bin")
out = run(store, item)
check("a stored filename names the saved file", out.kind == Outcome.DONE and Path(out.dest_file) == lib / "Files" / "mine.bin"
      and Path(out.dest_file).read_bytes() == srv.payload(1000))
item = claim(store, BASE + "/file.bin?size=2000", filename="mine.bin")
out2 = run(store, item)
check("a second download with the same filename never overwrites (unique_path)",
      out2.kind == Outcome.DONE and Path(out2.dest_file).name == "mine (1).bin" and Path(out.dest_file).read_bytes() == srv.payload(1000))
item = claim(store, BASE + "/cd?size=1000", filename="chosen.bin")
out3 = run(store, item)
check("the owner's filename beats the server's Content-Disposition", out3.kind == Outcome.DONE and Path(out3.dest_file).name == "chosen.bin")
item = claim(store, BASE + "/file.bin?size=1000", filename="clip.mkv")
out4 = run(store, item)
check("the kind folder still follows the chosen name (an .mkv goes under Video/)", Path(out4.dest_file) == lib / "Video" / "clip.mkv")
item = claim(store, BASE + "/file.bin?size=1000", filename="x" * 150 + ".bin")
out5 = run(store, item)
check("an over-long stored name (written by something other than the hub) still stays inside the library root",
      out5.kind == Outcome.DONE and Path(out5.dest_file).parent == lib / "Files" and len(Path(out5.dest_file).name) <= 130)
item = claim(store, BASE + "/file.bin?size=1000", filename="../../escape.bin")
out6 = run(store, item)
check("a hostile stored filename (written around the hub's check) cannot leave the library root",
      out6.kind == Outcome.DONE and lib in Path(out6.dest_file).resolve().parents and not (Path(HOME) / "escape.bin").exists())

store, lib = env()
item = claim(store, BASE + "/file.bin?size=1000")
run(store, item)
check("without a referer option no Referer header is sent", "Referer" not in STATE.headers[0])
store, lib = env()
item = claim(store, BASE + "/file.bin?size=1000", referer="https://example.com/the-page")
out = run(store, item)
check("a stored referer is sent on the first request", out.kind == Outcome.DONE and STATE.headers[0].get("Referer") == "https://example.com/the-page")
store, lib = env()
item = claim(store, BASE + "/file.bin?size=1000", referer="https://example.com/the-page", headers={"Referer": "https://other.example/", "Authorization": "Bearer X"})
run(store, item)
check("the referer option wins over a Referer in headers (headers still only come from addons)",
      STATE.headers[0].get("Referer") == "https://example.com/the-page")
store, lib = env()
item = claim(store, BASE + "/file.bin?size=1000", referer="https://example.com/a\r\nX-Evil: 1")
run(store, item)
check("a stored referer with a line break (written around the hub's check) is not sent",
      all("X-Evil" not in k for k in STATE.headers[0]) and STATE.headers[0].get("Referer") in (None, ""))
store, lib = env()
item = claim(store, BASE + "/redirect-host?size=1000", referer="https://example.com/the-page")
out = run(store, item)
check("TODAY's behaviour kept (the existing engine test pins it): Referer is not among the headers dropped on a host change",
      out.kind == Outcome.DONE and set(http_engine._DROP_ON_NEW_HOST) == {"authorization", "cookie"}
      and len(STATE.headers) >= 2 and all(h.get("Referer") == "https://example.com/the-page" for h in STATE.headers))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
