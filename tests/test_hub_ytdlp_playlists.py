"""Download Hub, brief 21: yt-dlp playlists as ONE GROUP, and the generic extractor asked once.

No network, no yt-dlp, no ffmpeg needed for almost all of it.  The probe is the real worker process
(or a small fake script standing in for it), the add tool is the real `_ytdlp_playlist.tool` called
with the real `_queue` of actions/download_hub.py (read-only here), the queue is a real Store.

Covers: what a link is (video / video inside a playlist / playlist or channel) and the link a
playlist is looked at through; the probe (a playlist, a single video, a failing helper, a helper that
dies, a helper that hangs and is killed, a pipe that lies); the tool: a playlist link queues
NOTHING until playlist="all" (title, size, first five titles, the exact call), "all" makes ONE
group named after the playlist with the same options on every row and never more than 50, a lower
cap, "one", a video that sits inside a playlist, a probe that fails, dry runs; the generic extractor
(one plain answer, switched off by default, and the three safety conditions with the switch
on, remembered on the row, refused again when the engine runs); the router's one-call approval; the
worker's probe and its generic guard (non-public addresses refused on every connect, streams that
another program would fetch refused).

NOT run here: real yt-dlp.  The last section needs yt-dlp (and the plugin extractor of
tests/_hub_ytdlp_media.py); without it that line is a clear "skip".

Run: python tests/test_hub_ytdlp_playlists.py
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-playlists-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_CHANNEL_SENDER", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.hub import engines, router, service  # noqa: E402
from jarvis.hub.engines import _ytdlp_playlist as pl  # noqa: E402
from jarvis.hub.engines import _ytdlp_worker as worker  # noqa: E402
from jarvis.hub.engines import ytdlp  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
import jarvis.ui_bridge as ui  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


# --------------------------------------------------------------------------- a fake worker
LOG = Path(HOME) / "worker-args.jsonl"
FAKE = Path(HOME) / "fake_worker.py"
FAKE.write_text(r'''
import json, os, sys, time
MARK = "@@JARVIS_YTDL_RESULT@@"
args = json.loads(sys.stdin.read())
with open(os.environ["FAKE_ARGS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
url = args["url"]

def result(payload):
    print("noise yt-dlp would never print")
    print(MARK + " " + json.dumps(payload))
    sys.exit(0)

def vid(i):
    return "https://www.youtube.com/watch?v=%011d" % i

def bigvid(i):
    return "https://www.youtube.com/watch?v=%011d" % (5000 + i)

if args.get("op") == "probe":
    if "PLbig" in url:
        result({"ok": True, "is_playlist": True, "title": "Big list", "count": 120, "more": True,
                "entries": [{"title": "Video %d" % i, "url": bigvid(i)} for i in range(100)]})
    if "PLgone" in url:
        result({"ok": True, "is_playlist": True, "title": "Gone", "count": 2, "entries": [], "unavailable": 2})
    if "PLone" in url:
        result({"ok": True, "is_playlist": False, "title": "Just a video"})
    if "PLfail" in url:
        result({"ok": False, "error": "ERROR: This playlist is private"})
    if "PLdie" in url:
        sys.exit(3)
    if "PLhang" in url:
        time.sleep(60)
    if "PLlie" in url:
        result({"ok": True, "is_playlist": True, "title": "x" * 500, "count": -5, "entries": [
            {"title": "ok", "url": vid(1)}, {"title": "dup", "url": vid(1)}, {"title": "file", "url": "file:///etc/passwd"},
            {"title": "js", "url": "javascript:alert(1)"}, "junk", {"title": "none"}] + [{"title": "e", "url": vid(100 + i)} for i in range(200)]})
    n = int(url.split("n=")[1].split("&")[0]) if "n=" in url else 37
    result({"ok": True, "is_playlist": True, "title": "My List", "count": n,
            "entries": [{"title": "Video number %d" % i, "url": vid(i)} for i in range(min(n, 100))]})

out = args["output_dir"]
if url.endswith("/gen"):
    with open(os.path.join(out, "Generic Page [x].mp4"), "wb") as f:
        f.write(b"x" * 100)
    result({"ok": True, "files": [{"file": "Generic Page [x].mp4", "title": "Generic Page", "bytes": 100}]})
result({"ok": False, "error": "unexpected url " + url})
''')
os.environ["FAKE_ARGS_LOG"] = str(LOG)
ytdlp.worker_command_override = [sys.executable, str(FAKE)]
ytdlp.ENGINE.available = lambda: (True, "")


def worker_calls():
    if not LOG.exists():
        return []
    return [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]


def reset_log():
    if LOG.exists():
        LOG.unlink()


store = Store()
service._store = lambda s=None: s or store           # the tools and add_items use THIS store


def rows():
    return service.list_items(limit=500).get("items", [])


def reset_rows():
    for r in rows():
        store.delete_item(r["id"]) if hasattr(store, "delete_item") else None


LIST = "https://www.youtube.com/playlist?list=PLabc"
MIXED = "https://www.youtube.com/watch?v=abcdefghijk&list=PLabc"
ONE = "https://www.youtube.com/watch?v=abcdefghijk"


class Names:
    def __getattr__(self, n):
        return n


# --------------------------------------------------------------------------- what a link is
for url, kind in ((ONE, "video"), (MIXED, "mixed"), (LIST, "playlist"),
                  ("https://youtu.be/abcdefghijk", "video"), ("https://youtu.be/abcdefghijk?list=PLabc", "mixed"),
                  ("https://www.youtube.com/@someone", "playlist"), ("https://www.youtube.com/@someone/videos", "playlist"),
                  ("https://www.youtube.com/channel/UC123", "playlist"), ("https://www.youtube.com/results?search_query=x", "playlist"),
                  ("https://www.youtube.com/watch?list=PLabc", "playlist"), ("https://www.youtube.com/shorts/abcdefghijk", "video"),
                  ("https://music.youtube.com/playlist?list=OLAK5", "playlist"), ("not a url", "video")):
    check("link_kind(%s) is %s" % (url[-40:], kind), pl.link_kind(url) == kind)
check("a watch?v=X&list=Y link is looked at through the playlist's own page",
      pl.playlist_url(MIXED) == "https://www.youtube.com/playlist?list=PLabc")
check("a plain playlist link is looked at as it is", pl.playlist_url(LIST) == LIST)
check("a non-YouTube link is left alone", pl.playlist_url("http://x.example/a?list=1") == "http://x.example/a?list=1")


class _FakeIE:
    def __init__(self, name, pattern):
        self.IE_NAME, self._p = name, pattern

    @classmethod
    def ie_key(cls):
        return "Fake"

    def suitable(self, url):
        return self._p in url


ytdlp.EXTRACTOR_SOURCE = lambda: [type("A", (), {"IE_NAME": "hubtest", "ie_key": staticmethod(lambda: "HubTest"),
                                                 "suitable": staticmethod(lambda u: "/hubtest/" in u)}),
                                  type("B", (), {"IE_NAME": "hubtest:list", "ie_key": staticmethod(lambda: "HubTestList"),
                                                 "suitable": staticmethod(lambda u: "/hubtestlist/" in u)})]
check("another site's playlist is recognised by its extractor's NAME",
      pl.link_kind("http://site.example/hubtestlist/a") == "playlist" and pl.link_kind("http://site.example/hubtest/a") == "video")

check("playlist argument: unset / one / all / junk",
      pl.parse_playlist_arg(None) == (None, None) and pl.parse_playlist_arg(" ALL ") == ("all", None)
      and pl.parse_playlist_arg("one") == ("one", None) and pl.parse_playlist_arg("some")[1] and pl.parse_playlist_arg("")[0] is None)
check("the cap is 50 unless a lower number is set, never higher",
      pl.PLAYLIST_CAP == 50 and pl.effective_cap({}) == 50 and pl.effective_cap({"ytdlp": {"playlist_max": 10}}) == 10
      and pl.effective_cap({"ytdlp": {"playlist_max": 500}}) == 50 and pl.effective_cap({"ytdlp": {"playlist_max": 0}}) == 1
      and pl.effective_cap({"ytdlp": {"playlist_max": "x"}}) == 50)
check("the schema pieces: playlist is one / all, generic is a boolean",
      pl.TOOL_PROPS["playlist"]["enum"] == ["one", "all"] and pl.TOOL_PROPS["generic"]["type"] == "boolean")

# --------------------------------------------------------------------------- the probe
reset_log()
p = pl.probe_playlist("https://www.youtube.com/playlist?list=PLabc")
check("the probe answers a playlist's title, size and entries",
      p["ok"] and p["is_playlist"] and p["title"] == "My List" and p["count"] == 37 and len(p["entries"]) == 37)
call = worker_calls()[0]
check("the helper was asked to PROBE (nothing downloaded) with the 100-entry ceiling",
      call["op"] == "probe" and call["max_entries"] == 100 and "output_dir" not in call)
p = pl.probe_playlist("https://www.youtube.com/playlist?list=PLone")
check("a link that is one video says so", p["ok"] and p["is_playlist"] is False)
p = pl.probe_playlist("https://www.youtube.com/playlist?list=PLfail")
check("a failing probe is an error with yt-dlp's words", p["ok"] is False and "private" in p["error"])
p = pl.probe_playlist("https://www.youtube.com/playlist?list=PLdie")
check("a helper that dies is an error, not a hang", p["ok"] is False and "stopped unexpectedly" in p["error"])
began = time.time()
p = pl.probe_playlist("https://www.youtube.com/playlist?list=PLhang", timeout=0.6)
check("a helper that hangs is killed after the time limit", p["ok"] is False and "longer than" in p["error"]
      and time.time() - began < 8)
p = pl.probe_playlist("https://www.youtube.com/playlist?list=PLlie")
check("the pipe is not trusted: only http(s), no duplicates, a title cut short, a count never below the entries",
      p["ok"] and all(e["url"].startswith("https://") for e in p["entries"]) and len({e["url"] for e in p["entries"]}) == len(p["entries"])
      and len(p["title"]) <= 120 and p["count"] >= len(p["entries"]) and not any("passwd" in e["url"] for e in p["entries"]))
check("...and never more entries than the ceiling", len(p["entries"]) <= pl.PROBE_MAX_ENTRIES)

# --------------------------------------------------------------------------- the tool
VIDEO = {"quality": "720p", "container": "mkv"}
ui_calls = []
ui.confirm = lambda *a, **k: (ui_calls.append(a[0]) or True)     # the bulk question: the owner says yes


def tool(args, urls=None, video=VIDEO):
    args = dict(args)
    return pl.tool(args, urls if urls is not None else [args["url"]], dict(video), dh._queue)


reset_log()
before = len(rows())
r = tool({"url": LIST})
check("a playlist link with no `playlist` queues NOTHING", len(rows()) == before and r.get("queued") == 0
      and r.get("needs_clarification") is True and r.get("ok") is False)
check("...it says the title and the size", r["playlist"]["title"] == "My List" and r["playlist"]["count"] == 37
      and "My List" in r["message"] and "37" in r["message"])
check("...it shows the first FIVE titles only", r["playlist"]["first_titles"] == ["Video number %d" % i for i in range(5)])
check("...and the exact call to make to take them", r["call"] == {"tool": "dl_add_ytdlp", "arguments": {"url": LIST, "playlist": "all"}}
      and 'playlist="all"' in r["message"])
check("...the answer stays small (no 37 titles)", len(json.dumps(r)) < 1500 and "Video number 30" not in json.dumps(r))
check("...one probe was made and nothing else", [c.get("op") for c in worker_calls()] == ["probe"])

reset_log()
r = tool({"url": LIST, "playlist": "one"})
check("`one` on a playlist page has nothing to take: nothing queued, nothing probed",
      len(rows()) == before and r.get("needs_clarification") and not worker_calls())
r = tool({"urls": [LIST, ONE]}, [LIST, ONE])
check("several links of which one is a playlist: nothing queued, the playlist is named",
      len(rows()) == before and r.get("needs_clarification") and r.get("playlist_links") == [LIST])

reset_log()
ui_calls.clear()
r = tool({"url": LIST, "playlist": "all"})
got = [x for x in rows() if x["engine"] == "ytdlp"]
check("playlist=all queues the whole list (37) as ONE group named after the playlist",
      r.get("ok") and r["count"] == 37 and len(got) == 37 and r["group"] and len({x.get("group_id") or x.get("group") for x in got}) == 1)
check("...the answer carries counts and a few rows, not 37",
      r["playlist"] == {"title": "My List", "count": 37, "more": False, "queued": 37, "not_taken": 0, "cap": 50, "unavailable": 0}
      and len(r["added"]) == 5 and r["added_not_shown"] == 32 and len(json.dumps(r)) < 3500)
check("...every row is one video of its own", all("watch?v=" in x["source_url"] and "list=" not in x["source_url"] for x in got)
      if "source_url" in got[0] else True)
first = store.get_item(got[0]["id"])
check("...every row carries the same video options (and not `playlist`)",
      all(store.get_item(x["id"])["options"].get("quality") == "720p" and store.get_item(x["id"])["options"].get("container") == "mkv"
          and "playlist" not in store.get_item(x["id"])["options"] for x in got))
listed = service.list_items(group=r["group"], limit=100)
check("dl_list group=... shows exactly that group's 37 rows", len(listed["items"]) == 37)
check("the bulk question was asked once for the group (the existing one)", len(ui_calls) == 1)
check("a second call is no new rows (the hub dedupes)", tool({"url": LIST, "playlist": "all"})["added"][0]["duplicate"] is True)

# over the cap
reset_log()
ui_calls.clear()
big = "https://www.youtube.com/playlist?list=PLbig"
before = len(rows())
r = tool({"url": big, "playlist": "all"})
check("a playlist of 120 queues 50 and says how many were not taken",
      r["count"] == 50 and len(rows()) - before == 50 and r["playlist"]["not_taken"] == 70 and r["playlist"]["more"] is True
      and "limit is 50" in r["note"] and "At least" in r["note"])
check("the group is named after the playlist", service.list_items(group=r["group"], limit=100)["items"].__len__() == 50)
check("never more than 50 rows from one call, whatever the probe said", pl.PLAYLIST_CAP == 50)

# a playlist that is all private
r = tool({"url": "https://www.youtube.com/playlist?list=PLgone", "playlist": "all"})
check("a playlist with nothing queueable is an error, nothing queued", r["ok"] is False and "no video I can queue" in r["error"])
# one video after all
reset_log()
before = len(rows())
r = tool({"url": "https://www.youtube.com/playlist?list=PLone", "playlist": "all"})
check("`all` on a link that turns out to be one video queues it as one", r["ok"] and r["count"] == 1 and "one video" in r["note"])
# a probe that fails
before = len(rows())
r = tool({"url": "https://www.youtube.com/playlist?list=PLfail", "playlist": "all"})
check("a probe that fails queues nothing and says so", r["ok"] is False and len(rows()) == before and "nothing was queued" in r["error"])
r = tool({"url": "https://www.youtube.com/playlist?list=PLfail"})
check("...also without `playlist` (it is not queued whole as a fallback)", r["ok"] is False and len(rows()) == before)
r = pl.tool({"playlist": "all"}, [LIST, big], dict(VIDEO), dh._queue)
check("...a list of two with all is refused", r["ok"] is False and "one playlist link per call" in r["error"])
check("a bad `playlist` value is refused", pl.tool({"playlist": "some"}, [LIST], {}, dh._queue)["ok"] is False)

# a link that names a video and a playlist
reset_log()
before = len(rows())
r = tool({"url": MIXED})
check("watch?v=X&list=Y queues the ONE video, as it always did, with a note on the playlist",
      r["ok"] and r["count"] == 1 and "playlist" in r["note"] and 'playlist="all"' in r["note"] and not worker_calls())
check("...the row keeps the link as given", store.get_item(r["added"][0]["id"])["source_url"] == MIXED)
reset_log()
r = tool({"url": MIXED, "playlist": "one"})
check("...`one` says nothing more about the playlist", r["ok"] and "playlist" not in (r.get("note") or ""))
r = tool({"url": MIXED, "playlist": "all"})
check("...`all` looks at the playlist's own page", r["ok"] and worker_calls()[0]["url"] == "https://www.youtube.com/playlist?list=PLabc")

# dry runs
reset_log()
before = len(rows())
r = tool({"url": LIST, "playlist": "all", "dry_run": True})
check("a dry run looks at nothing and queues nothing", r["dry_run"] is True and not worker_calls() and len(rows()) == before)
r = tool({"url": LIST, "dry_run": True})
check("...also with no `playlist`; the note says a real call asks first", r["dry_run"] is True and not worker_calls()
      and "asks before" in r["note"])

# guests are refused by the tool before this code runs, but the hook sits after that
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
check("the add tool still refuses a chat guest first (the hook sits after the check)",
      dh.tool_dl_add_ytdlp({"url": LIST, "playlist": "all"}).get("ok") is False)
os.environ.pop("JARVIS_CHANNEL_SENDER", None)

# --------------------------------------------------------------------------- through the real tool (once its hook is in)
_schema = [t for t in dh.TOOL_SCHEMAS if t["name"] == "dl_add_ytdlp"][0]["parameters"]["properties"]
if "playlist" in _schema and "generic" in _schema:
    reset_log()
    before = len(rows())
    r = dh.tool_dl_add_ytdlp({"url": LIST})
    check("dl_add_ytdlp itself: a playlist link queues nothing and asks (the hook is in)",
          r.get("needs_clarification") is True and len(rows()) == before and _schema["playlist"]["enum"] == ["one", "all"])
    check("dl_add (the generic tool) has neither argument", not ({"playlist", "generic"} &
          set([t for t in dh.TOOL_SCHEMAS if t["name"] == "dl_add"][0]["parameters"]["properties"])))
else:
    print("skip    dl_add_ytdlp itself: its schema and handler do not call this module yet (the hook is in "
          "actions/download_hub.py, agent B's file; see the report)")

# --------------------------------------------------------------------------- generic: switched off
reset_log()
UNKNOWN = "http://93.184.216.34/some/page"
before = len(rows())
check("the switch is OFF in this version", ytdlp.GENERIC_ENABLED is False)
r = tool({"url": UNKNOWN})
check("a link no extractor claims gets ONE plain answer and nothing is queued",
      r["ok"] is False and r["needs_clarification"] and r["queued"] == 0 and len(rows()) == before
      and "no specific support" in r["message"] and r["generic_available"] is False and "call" not in r)
r = tool({"url": UNKNOWN, "generic": True})
check("generic=true with the switch off is refused, nothing queued",
      r["ok"] is False and "switched off" in r["error"] and len(rows()) == before)
check("...and the options check refuses it too (add_items would)", ytdlp.normalize_options({"generic": True})[1])
check("generic=false stores nothing", ytdlp.normalize_options({"generic": False}) == ({}, None))
r = tool({"urls": [ONE, UNKNOWN]}, [ONE, UNKNOWN])
check("one claimed and one unclaimed link: the claimed one is queued, the other is named",
      r["ok"] and r["count"] == 1 and r["unsupported"] == [UNKNOWN] and "no specific support" in r["note"])
check("the engine itself never took a link the router did not claim",
      router.route(UNKNOWN, engine="ytdlp")["ok"] is False and router.route(UNKNOWN)["engine"] != "ytdlp")

# --------------------------------------------------------------------------- generic: switch ON (the conditions)
ytdlp.GENERIC_ENABLED = True
try:
    reset_log()
    before = len(rows())
    r = tool({"url": UNKNOWN})
    check("with the switch on, the plain answer gives the exact call, and still queues nothing",
          r["ok"] is False and r["generic_available"] is True and r["call"] == {"tool": "dl_add_ytdlp", "arguments": {"url": UNKNOWN, "generic": True}}
          and len(rows()) == before)
    check("the router still refuses it without the one-call approval", router.route(UNKNOWN, engine="ytdlp")["ok"] is False)
    r = tool({"url": UNKNOWN, "generic": True})
    row = store.get_item(r["added"][0]["id"]) if r.get("added") else {}
    check("generic=true on a public http link queues it on the yt-dlp engine, ONE row",
          r.get("ok") and r["count"] == 1 and row.get("engine") == "ytdlp" and r["added"][0]["engine"] == "ytdlp")
    check("...the row remembers it, so a retry never asks again", row.get("options", {}).get("generic") is True)
    check("...the approval ended with the call", router.route(UNKNOWN, engine="ytdlp")["ok"] is False)

    seen = {}

    def other_thread():
        seen["r"] = router.route(UNKNOWN, engine="ytdlp")["ok"]
    with router.generic_allowed([UNKNOWN]):
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        check("the approval is this thread's, for this exact link",
              router.route(UNKNOWN, engine="ytdlp")["ok"] is True and seen["r"] is False
              and router.route(UNKNOWN + "2", engine="ytdlp")["ok"] is False
              and router.route(UNKNOWN, engine="http")["engine"] in ("http", None))
    for bad, why in (("http://10.1.2.3/p", "private"), ("http://127.0.0.1:8080/p", "private"), ("http://[::1]/p", "private"),
                     ("http://169.254.169.254/latest", "private"), ("ftp://93.184.216.34/p", "only http"),
                     ("file:///etc/passwd", "only http"), ("http://user:pw@93.184.216.34/p", "password"),
                     ("http://93.184.216.34/movie.mp4", "file link")):
        r = tool({"url": bad, "generic": True})
        check("generic=true refuses %s (%s)" % (bad, why), r["ok"] is False and len(rows()) == before + 1)
    check("...a hostname that resolves to a private address is refused too",
          ytdlp.generic_problem("http://localhost:9999/p") is not None)
    r = tool({"urls": [UNKNOWN, UNKNOWN + "2"], "generic": True}, [UNKNOWN, UNKNOWN + "2"])
    check("generic=true takes ONE link per call", r["ok"] is False and "exactly one link" in r["error"])
    r = tool({"url": LIST, "generic": True, "playlist": "all"})
    check("generic and playlist=all do not mix", r["ok"] is False and "can't be combined" in r["error"])
    r = tool({"url": ONE, "generic": True})
    check("a link a specific extractor claims is queued normally, no generic flag",
          r["ok"] and "options" not in r and store.get_item(r["added"][0]["id"])["options"].get("generic") is None
          and "not needed" in r["note"])

    # the engine, retrying: the row says generic, the helper is told, the link is checked AGAIN
    reset_log()
    estore = Store(Path(HOME) / "engine.db")
    estore.set_settings({"library_root": str(Path(HOME) / "lib")})
    item = estore.add_item(engine="ytdlp", source_url="http://93.184.216.34/gen", title="x",
                           options={"generic": True, "quality": "720p"})[0]
    claimed = estore.claim_next({"default": 100, "ytdlp": 100}, 100)
    ctx = EngineContext(estore, claimed, estore.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    out = ytdlp.ENGINE.run(claimed, ctx)
    sent = worker_calls()
    check("the engine tells the helper `generic: true` and the download goes through",
          out.kind == Outcome.DONE and sent and sent[0].get("generic") is True)
    ytdlp.GENERIC_ENABLED = False
    reset_log()
    estore.add_item(engine="ytdlp", source_url="http://93.184.216.34/gen2", title="x", options={"generic": True})
    claimed2 = estore.claim_next({"default": 100, "ytdlp": 100}, 100)
    out = ytdlp.ENGINE.run(claimed2, EngineContext(estore, claimed2, estore.get_settings(), stop_reason=lambda: None,
                                                    rate_limit_bps=lambda: 0))
    check("switched off again, a stored `generic` row fails for good and the helper is never started",
          out.kind == Outcome.FAILED and not out.retryable and "generic" in out.error and not worker_calls())
finally:
    ytdlp.GENERIC_ENABLED = False
check("a refusal by the guard is classed permanent (never retried)",
      ytdlp.classify_error("download failed: <urlopen error 10.0.0.5 is a private or local address: refused>") == (False, None))

# --------------------------------------------------------------------------- the worker: probe, in this process
class FakeYDL:
    info = None
    seen = None

    def __init__(self, opts):
        FakeYDL.seen = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if isinstance(FakeYDL.info, Exception):
            raise FakeYDL.info
        return FakeYDL.info


fake_mod = types.ModuleType("yt_dlp")
fake_mod.YoutubeDL = FakeYDL
sys.modules["yt_dlp"] = fake_mod
try:
    FakeYDL.info = {"_type": "playlist", "title": "T", "playlist_count": 9, "entries": [
        {"title": "a", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
        {"title": "[Private video]", "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb"},
        {"title": "[Deleted video]", "url": "https://www.youtube.com/watch?v=ccccccccccc"},
        {"title": "bare", "url": "ddddddddddd", "ie_key": "Youtube"},
        {"title": "rel", "url": "/watch?v=eeeeeeeeeee"},
        "junk", {"title": "web", "webpage_url": "https://site.example/v/1"}]}
    res = worker.probe({"url": "http://x/y", "max_entries": 500})
    check("worker probe: flat extraction, playlist end at the ceiling, nothing downloaded",
          FakeYDL.seen["extract_flat"] == "in_playlist" and FakeYDL.seen["skip_download"] is True
          and FakeYDL.seen["playlistend"] == 100 and FakeYDL.seen.get("noplaylist") is False)
    check("worker probe: usable entries, bare YouTube ids made into links, private / deleted counted apart",
          [e["url"] for e in res["entries"]] == ["https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=ddddddddddd",
                                                 "https://site.example/v/1"]
          and res["unavailable"] == 2 and res["unusable"] == 2)
    check("worker probe: the size is yt-dlp's count when it knows one, and `more` when it is bigger",
          res["count"] == 9 and res["more"] is True)
    FakeYDL.info = {"_type": "playlist", "title": "T", "entries": [{"title": str(i), "url": "https://s.example/%d" % i} for i in range(300)]}
    res = worker.probe({"url": "http://x/y", "max_entries": 40})
    check("worker probe: an unknown size with a full page says `more`, and respects the cap",
          len(res["entries"]) == 40 and res["more"] is True and res["count"] == 40)
    FakeYDL.info = {"title": "Single", "formats": []}
    check("worker probe: a single video is not a playlist", worker.probe({"url": "http://x/y"}) == {"ok": True, "is_playlist": False, "title": "Single"})
    FakeYDL.info = RuntimeError("HTTP Error 404")
    check("worker probe: a failure is an answer", worker.probe({"url": "http://x/y"})["ok"] is False)
    check("worker probe: no url is an answer", worker.probe({})["ok"] is False)
finally:
    del sys.modules["yt_dlp"]

# --------------------------------------------------------------------------- the worker: the generic guard
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(5)
port = srv.getsockname()[1]
accepted = []
threading.Thread(target=lambda: [accepted.append(srv.accept()) for _ in range(5)], daemon=True).start()

restore = worker.install_network_guard()
try:
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
        refused = False
    except OSError as exc:
        refused = "private or local address" in str(exc)
    finally:
        s.close()
    check("guard: a connect to a loopback address is refused, and says why", refused)
    s = socket.socket()
    check("guard: connect_ex reports it instead of raising", s.connect_ex(("127.0.0.1", port)) == 13)
    s.close()
    try:
        socket.create_connection(("localhost", port), timeout=2)
        named = False
    except OSError as exc:
        named = "private or local address" in str(exc)
    check("guard: a NAME that resolves to a loopback address is refused (create_connection)", named)
    for addr in (("10.0.0.5", 80), ("192.168.1.1", 80), ("169.254.169.254", 80), ("::ffff:127.0.0.1", 80)):
        fam = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        try:
            s = socket.socket(fam)
        except OSError:
            print("skip    guard: %s (this machine has no IPv6 sockets)" % addr[0])
            continue
        try:
            s.settimeout(0.2)
            s.connect(addr)
            ok = False
        except OSError as exc:
            ok = "private or local address" in str(exc)
        finally:
            s.close()
        check("guard: %s is refused before any packet is sent" % addr[0], ok)
    for mapped in ("::ffff:127.0.0.1", "::ffff:10.0.0.5", "::1", "fe80::1"):
        try:
            worker._peer_check(types.SimpleNamespace(family=socket.AF_INET6), (mapped, 80))
            ok = False
        except OSError as exc:
            ok = "private or local address" in str(exc)
        check("guard: %s (an IPv6 form) is refused, without needing an IPv6 socket" % mapped, ok)
    check("guard: a local (unix) socket is not the network", worker._peer_check(types.SimpleNamespace(family=socket.AF_UNIX), "/tmp/x") == "/tmp/x")
finally:
    restore()
restore2 = worker.install_network_guard(is_public=lambda ip: True)
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.close()
    check("guard: a public address goes through (the check is the only thing in the way)", True)
finally:
    restore2()
s = socket.create_connection(("127.0.0.1", port), timeout=2)
s.close()
check("guard: undone, connections work as before", True)
srv.close()

for info, bad in (({"protocol": "https"}, False), ({"protocol": "http"}, False), ({"protocol": "m3u8_native"}, False),
                  ({"protocol": "http_dash_segments"}, False), ({}, False), ({"protocol": "m3u8"}, True),
                  ({"protocol": "rtsp"}, True), ({"protocol": "mms"}, True), ({"protocol": "rtmp"}, True),
                  ({"protocol": "https", "is_live": True}, True)):
    try:
        worker.check_generic_format(info)
        refused = False
    except RuntimeError:
        refused = True
    check("generic format check: %s is %s" % (info or "no protocol", "refused" if bad else "allowed"), refused == bad)


class BaseYDL:
    def __init__(self, opts=None):
        self.fetched = []

    def dl(self, name, info, subtitle=False, test=False):
        self.fetched.append(info.get("protocol"))
        return "done"


mod = types.ModuleType("yt_dlp")
mod.YoutubeDL = BaseYDL
undo = worker.apply_generic_guard(mod)
try:
    y = mod.YoutubeDL()
    check("generic guard: a plain https file is fetched by the real downloader", y.dl("f", {"protocol": "https"}) == "done" and y.fetched == ["https"])
    try:
        y.dl("f", {"protocol": "rtsp"})
        blocked = False
    except RuntimeError:
        blocked = True
    check("generic guard: an rtsp stream is refused BEFORE anything is fetched", blocked and y.fetched == ["https"])
    check("generic guard: the network guard is on", worker._peer_check.__name__ == "_peer_check"
          and socket.socket.connect.__name__ == "connect" and socket.socket.connect is not socket.socket.__mro__[1].connect)
finally:
    undo()
check("generic guard: undone", mod.YoutubeDL is BaseYDL)

# --------------------------------------------------------------------------- the real worker process, a fake yt_dlp
FAKE_PKG = Path(HOME) / "fakepkg"
(FAKE_PKG / "yt_dlp").mkdir(parents=True)
(FAKE_PKG / "yt_dlp" / "__init__.py").write_text('''
class YoutubeDL:
    def __init__(self, opts): self.opts = opts
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=False):
        assert self.opts["extract_flat"] == "in_playlist" and download is False
        return {"_type": "playlist", "title": "From the real worker", "playlist_count": 2,
                "entries": [{"title": "one", "url": "https://s.example/1"}, {"title": "two", "url": "https://s.example/2"}]}
''')
old_cmd, old_pp = ytdlp.worker_command_override, os.environ.get("PYTHONPATH")
ytdlp.worker_command_override = None
os.environ["PYTHONPATH"] = str(FAKE_PKG)
try:
    p = pl.probe_playlist("http://s.example/list", timeout=30)
    check("the REAL worker process answers a probe over the pipe (with a fake yt_dlp)",
          p.get("ok") and p["title"] == "From the real worker" and p["count"] == 2 and len(p["entries"]) == 2)
finally:
    ytdlp.worker_command_override = old_cmd
    if old_pp is None:
        os.environ.pop("PYTHONPATH", None)
    else:
        os.environ["PYTHONPATH"] = old_pp

# --------------------------------------------------------------------------- nothing else moved
check("the options a hub row may carry still leave `playlist` out", not ({"playlist", "output_dir", "cookies", "generic"} & set(ytdlp.PASS_KEYS)))
check("YouTube and site routes are unchanged",
      router.route(ONE)["engine"] == "ytdlp" and router.route(LIST)["engine"] == "ytdlp"
      and router.route("http://site.example/hubtest/a")["engine"] == "ytdlp"
      and router.route("http://127.0.0.1:1/file.bin")["engine"] == "http")

# --------------------------------------------------------------------------- real yt-dlp (not run without it)
import importlib.util  # noqa: E402
if importlib.util.find_spec("yt_dlp") is None:
    print("skip    real yt-dlp playlist probe against the plugin extractor (yt-dlp isn't installed)")
else:
    import _hub_ytdlp_media as media  # noqa: E402
    plug = Path(HOME) / "plugin"
    media.write_plugin(str(plug))
    ytdlp.worker_command_override = None
    old_pp = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(plug)
    try:
        p = pl.probe_playlist("http://127.0.0.1:9/hubtestlist/demo?n=7", timeout=60)
        check("real yt-dlp: the plugin playlist has 7 entries and a title",
              p.get("ok") and p["is_playlist"] and p["count"] == 7 and len(p["entries"]) == 7 and p["title"].startswith("Hub Test List"))
        check("real yt-dlp: every entry is a single-video link", all("/hubtest/demo-" in e["url"] for e in p["entries"]))
        p = pl.probe_playlist("http://127.0.0.1:9/hubtest/solo", timeout=60)
        check("real yt-dlp: a single video is not a playlist", p.get("ok") and p["is_playlist"] is False)
    finally:
        if old_pp is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pp

print(f"\n{passed} passed, {failed} failed")
shutil.rmtree(HOME, ignore_errors=True)
sys.exit(1 if failed else 0)
