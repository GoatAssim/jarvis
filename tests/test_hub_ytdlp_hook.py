"""Download Hub, brief 30: the hook from `dl_add_ytdlp` into the yt-dlp playlist logic.

Brief 21 wrote the logic (hub/engines/_ytdlp_playlist.py) and its own test calls `_ytdlp_playlist.tool`
directly.  This file goes through the TOOL: `download_hub.tool_dl_add_ytdlp`, the real `_queue`, a real
Store, and a small fake worker standing in for the yt-dlp helper process (the probe).  Nothing here
touches the network and nothing needs yt-dlp or ffmpeg.

Covers: a playlist or channel link with no `playlist` queues NOTHING and asks (title, size, at most five
titles, the exact call); `playlist="all"` queues one group of at most 50 rows (and fewer when the cap is
lowered); `playlist="one"` and a `watch?v=X&list=Y` link give the single video (the second with the note);
a plain video link gives the same rows and the same answer as the tool's old body (`_queue` alone);
`generic=true` while the generic extractor is switched off is one plain sentence and queues nothing; a
guest is refused before any playlist look-up; the schema's `playlist` / `generic` are exactly
`_ytdlp_playlist.TOOL_PROPS`, on `dl_add_ytdlp` only; `dl_add` does not take them.

NOT run here: real yt-dlp, real YouTube, the real probe (the helper process is a fake).

Run: python tests/test_hub_ytdlp_hook.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-ytdlp-hook-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_CHANNEL_SENDER", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.hub import service  # noqa: E402
from jarvis.hub.engines import _ytdlp_playlist as pl  # noqa: E402
from jarvis.hub.engines import ytdlp  # noqa: E402
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


# --------------------------------------------------------------------------- a fake worker (the probe only)
LOG = Path(HOME) / "worker-args.jsonl"
FAKE = Path(HOME) / "fake_worker.py"
FAKE.write_text(r'''
import json, os, sys
MARK = "@@JARVIS_YTDL_RESULT@@"
args = json.loads(sys.stdin.read())
with open(os.environ["FAKE_ARGS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
url = args["url"]

def result(payload):
    print(MARK + " " + json.dumps(payload))
    sys.exit(0)

def vid(i):
    return "https://www.youtube.com/watch?v=%011d" % i

if args.get("op") != "probe":
    result({"ok": False, "error": "the hook test never runs a download: " + url})
if "PLbig" in url:
    result({"ok": True, "is_playlist": True, "title": "Big list", "count": 120, "more": True,
            "entries": [{"title": "Big %d" % i, "url": vid((9000 if "PLbig2" in url else 5000) + i)} for i in range(100)]})
if "PLone" in url:
    result({"ok": True, "is_playlist": False, "title": "Just a video"})
if "PLfail" in url:
    result({"ok": False, "error": "ERROR: This playlist is private"})
if "@somechannel" in url:
    result({"ok": True, "is_playlist": True, "title": "Some Channel - Videos", "count": 12,
            "entries": [{"title": "Channel video %d" % i, "url": vid(7000 + i)} for i in range(12)]})
result({"ok": True, "is_playlist": True, "title": "My List", "count": 37,
        "entries": [{"title": "Video number %d" % i, "url": vid(i)} for i in range(37)]})
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
service._store = lambda s=None: s or store
ui_calls = []
ui.confirm = lambda *a, **k: (ui_calls.append(a[0]) or True)      # the bulk question: the owner says yes


def rows():
    return service.list_items(limit=500).get("items", [])


LIST = "https://www.youtube.com/playlist?list=PLabc"
BIG = "https://www.youtube.com/playlist?list=PLbig"
CHANNEL = "https://www.youtube.com/@somechannel"
MIXED = "https://www.youtube.com/watch?v=abcdefghijk&list=PLabc"
ONE = "https://www.youtube.com/watch?v=abcdefghijk"
VIDEO_OPTS = {"quality": "720p", "container": "mkv"}


def schema(name):
    return next(s for s in dh.TOOL_SCHEMAS if s["name"] == name)


def props(name):
    return schema(name)["parameters"]["properties"]


# --------------------------------------------------------------------------- the schema
check("TOOL_PROPS names exactly `playlist` and `generic`", set(pl.TOOL_PROPS) == {"playlist", "generic"})
check("the schema property names of dl_add_ytdlp include every TOOL_PROPS name",
      set(pl.TOOL_PROPS) <= set(props("dl_add_ytdlp")))
check("...and each schema entry is the same as TOOL_PROPS' (the file keeps a literal copy: they must not drift)",
      all(props("dl_add_ytdlp")[n] == pl.TOOL_PROPS[n] for n in pl.TOOL_PROPS))
check("`playlist` is one / all and `generic` is a boolean in the schema",
      props("dl_add_ytdlp")["playlist"]["enum"] == ["one", "all"] and props("dl_add_ytdlp")["generic"]["type"] == "boolean")
check("neither is required", not schema("dl_add_ytdlp")["parameters"].get("required"))
check("dl_add and dl_add_stremio do NOT get them (rule N4: no engine-specific argument on dl_add)",
      not ({"playlist", "generic"} & set(props("dl_add"))) and not ({"playlist", "generic"} & set(props("dl_add_stremio"))))
check("the description no longer says only 'one video per link': it says a playlist asks first",
      "asks first" in schema("dl_add_ytdlp")["description"] and "playlist" in schema("dl_add_ytdlp")["description"])
probe_import = subprocess.run(
    [sys.executable, "-c", "import sys; sys.path.insert(0, %r); import jarvis.actions.download_hub; "
     "print(sorted(m for m in sys.modules if m.startswith('jarvis.hub')))" % str(ROOT / "jarvis-cli")],
    capture_output=True, text=True, timeout=120)
check("loading the tool file still loads no part of the hub package (the schema is a literal copy for that reason)",
      probe_import.returncode == 0 and probe_import.stdout.strip().endswith("[]"))
check("the generic extractor is still switched OFF in this version", ytdlp.GENERIC_ENABLED is False)

# --------------------------------------------------------------------------- a playlist link asks, queues nothing
reset_log()
before = len(rows())
r = dh.tool_dl_add_ytdlp({"url": LIST})
check("a playlist link with no `playlist` queues NOTHING and asks",
      len(rows()) == before and r.get("queued") == 0 and r.get("needs_clarification") is True and r.get("ok") is False)
check("...it names the playlist and its size", r["playlist"]["title"] == "My List" and r["playlist"]["count"] == 37
      and "My List" in r["message"] and "37" in r["message"])
check("...at most FIVE titles", r["playlist"]["first_titles"] == ["Video number %d" % i for i in range(5)]
      and "Video number 5" not in json.dumps(r))
check("...and the exact call to make",
      r["call"] == {"tool": "dl_add_ytdlp", "arguments": {"url": LIST, "playlist": "all"}} and 'playlist="all"' in r["message"])
check("...the answer stays small", len(json.dumps(r)) < 1500)
check("...one probe and nothing else was run", [c.get("op") for c in worker_calls()] == ["probe"]
      and worker_calls()[0]["url"] == LIST)

reset_log()
r = dh.tool_dl_add_ytdlp({"url": CHANNEL})
check("a channel link asks too, and queues nothing",
      len(rows()) == before and r.get("needs_clarification") is True and r["playlist"]["title"] == "Some Channel - Videos"
      and r["playlist"]["count"] == 12 and len(r["playlist"]["first_titles"]) == 5)

reset_log()
r = dh.tool_dl_add_ytdlp({"urls": [LIST, ONE]})
check("two links, one of them a playlist: nothing queued, the playlist is named, no probe",
      len(rows()) == before and r.get("needs_clarification") and r.get("playlist_links") == [LIST] and not worker_calls())

reset_log()
r = dh.tool_dl_add_ytdlp({"url": LIST, "quality": "1080p", "mode": "audio"})
check("video options on a playlist link do not skip the question", len(rows()) == before and r.get("needs_clarification") is True)

reset_log()
r = dh.tool_dl_add_ytdlp({"url": LIST, "dry_run": True})
check("a dry run on a playlist link looks at nothing, queues nothing, and says a real call asks first",
      r.get("dry_run") is True and not worker_calls() and len(rows()) == before and "asks before" in r.get("note", ""))

reset_log()
r = dh.tool_dl_add_ytdlp({"url": "https://www.youtube.com/playlist?list=PLfail"})
check("a playlist that can't be looked at queues nothing and says why (it is not taken whole as a fallback)",
      r.get("ok") is False and len(rows()) == before and "private" in r.get("error", ""))

reset_log()
r = dh.tool_dl_add_ytdlp({"url": LIST, "playlist": "one"})
check("`one` on a PLAYLIST page has nothing to take: nothing queued, nothing probed",
      len(rows()) == before and r.get("needs_clarification") is True and not worker_calls())
r = dh.tool_dl_add_ytdlp({"url": LIST, "playlist": "some"})
check("a bad `playlist` value is refused with a sentence", r.get("ok") is False and "'one' or 'all'" in r.get("error", "")
      and len(rows()) == before)

# --------------------------------------------------------------------------- playlist = "all": one group
reset_log()
ui_calls.clear()
r = dh.tool_dl_add_ytdlp({"url": LIST, "playlist": "all", "quality": "720p", "container": "mkv", "start_paused": True})
got = [x for x in rows() if x["engine"] == "ytdlp"]
check("playlist=all queues the 37 videos as ONE group named after the playlist",
      r.get("ok") and r["count"] == 37 and len(got) == 37 and r["group"]
      and len(service.list_items(group=r["group"], limit=100)["items"]) == 37)
check("...every row is one video, with the options given (and no `playlist` option on the row)",
      all(store.get_item(x["id"])["options"].get("quality") == "720p" and store.get_item(x["id"])["options"].get("container") == "mkv"
          and "playlist" not in store.get_item(x["id"])["options"] for x in got))
check("...every row says it came from dl_add_ytdlp",
      all(a.get("add_tool") == "dl_add_ytdlp" for a in r["added"]))
check("...the answer carries counts and five rows, not 37",
      r["playlist"]["queued"] == 37 and r["playlist"]["cap"] == 50 and len(r["added"]) == 5 and r["added_not_shown"] == 32
      and len(json.dumps(r)) < 3500)
check("...the one probe asked for a LOOK (no download) and the bulk question was asked once",
      [c.get("op") for c in worker_calls()] == ["probe"] and len(ui_calls) == 1)

reset_log()
ui_calls.clear()
before = len(rows())
r = dh.tool_dl_add_ytdlp({"url": BIG, "playlist": "all", "start_paused": True})
check("a playlist of 120 queues 50, one group, and says 70 were not taken",
      r["count"] == 50 and len(rows()) - before == 50 and r["playlist"]["not_taken"] == 70 and "limit is 50" in r["note"]
      and len(service.list_items(group=r["group"], limit=100)["items"]) == 50)

# a lower cap (the setting `ytdlp.playlist_max` is not defined in hub/settings.py yet: it is stubbed here)
real_get_settings = service.get_settings


def lowered(*a, **k):
    out = real_get_settings(*a, **k)
    settings = dict(out.get("settings") or {})
    settings["ytdlp"] = dict(settings.get("ytdlp") or {}, playlist_max=10)
    return dict(out, settings=settings)


service.get_settings = lowered
try:
    before = len(rows())
    r = dh.tool_dl_add_ytdlp({"url": "https://www.youtube.com/playlist?list=PLbig2", "playlist": "all", "start_paused": True})
    check("a lower cap from the settings is honoured: 10 rows, never more",
          r["count"] == 10 and len(rows()) - before == 10 and r["playlist"]["cap"] == 10 and "limit is 10" in r["note"])
finally:
    service.get_settings = real_get_settings

# --------------------------------------------------------------------------- the single video
reset_log()
before = len(rows())
r = dh.tool_dl_add_ytdlp({"url": MIXED, "start_paused": True})
check("watch?v=X&list=Y queues the ONE video, with a note on how to take the playlist, and probes nothing",
      r["ok"] and r["count"] == 1 and 'playlist="all"' in r["note"] and not worker_calls() and len(rows()) == before + 1)
check("...the row keeps the link as given", store.get_item(r["added"][0]["id"])["source_url"] == MIXED)
reset_log()
r = dh.tool_dl_add_ytdlp({"url": "https://www.youtube.com/watch?v=zzzzzzzzzzz&list=PLabc", "playlist": "one", "start_paused": True})
check("`one` on that link queues the single video and the note about the playlist is not repeated",
      r["ok"] and r["count"] == 1 and "playlist" not in (r.get("note") or "") and not worker_calls())
reset_log()
r = dh.tool_dl_add_ytdlp({"url": "https://www.youtube.com/watch?v=yyyyyyyyyyy", "playlist": "one", "start_paused": True})
check("`one` on a plain video link is just the video", r["ok"] and r["count"] == 1 and not worker_calls())

# --------------------------------------------------------------------------- a plain video: as before
# "As before" = what the tool's old body did: `_queue` and nothing else.  The same call is made twice, once
# through `_queue` alone and once through the tool, with different video ids (the hub dedupes on the link, so
# the second call must not be the same link) and compared without the ids.
def normal(answer):
    """The answer and its rows without what differs run to run (ids)."""
    rows_ = [{k: v for k, v in row.items() if k != "id"} for row in answer.get("added", [])]
    rest = {k: v for k, v in answer.items() if k not in ("added", "group")}
    return rest, rows_


def stored(answer):
    return sorted((i["kind"], json.dumps(i["options"], sort_keys=True), i["engine"], i["state"])
                  for i in (store.get_item(x["id"]) for x in answer["added"]))


for label, build in (
        ("one video link with options", lambda m: {"url": "https://youtu.be/%s" % (m * 11), "mode": "audio",
                                                   "audio_format": "flac", "start_paused": True}),
        ("two links in one call", lambda m: {"urls": ["https://youtu.be/%s" % (m * 10 + "1"), "https://youtu.be/%s" % (m * 10 + "2")],
                                             "group": "Two", "quality": "480p", "start_paused": True}),
        ("a watch?v link", lambda m: {"url": "https://www.youtube.com/watch?v=%s" % (m * 11), "start_paused": True,
                                      "notify": "off"})):
    reset_log()
    old_args, new_args = build("p"), build("q")
    video = {k: old_args.get(k) for k in dh._VIDEO_ARGS if old_args.get(k) is not None}
    old_res = dh._queue(dict(old_args), dh._gather_urls(old_args), "ytdlp", video, "dl_add_ytdlp")
    new_res = dh.tool_dl_add_ytdlp(dict(new_args))
    o_ans, o_rows = normal(old_res)
    n_ans, n_rows = normal(new_res)
    check("a plain video (%s): the same answer shape and values as before" % label,
          old_res.get("ok") and o_ans == n_ans and o_rows == n_rows and len(n_rows) == len(o_rows) >= 1)
    check("...the same stored rows (kind, options, engine, state)", stored(old_res) == stored(new_res))
    check("...no playlist note, no playlist key, no probe", "playlist" not in (new_res.get("note") or "")
          and "playlist" not in new_res and not worker_calls())

reset_log()
r = dh.tool_dl_add_ytdlp({"url": ONE, "dry_run": True})
check("a dry run on a plain video link names the engine and the add tool, as before",
      r["dry_run"] is True and r["decisions"][0]["engine"] == "ytdlp" and r["decisions"][0]["add_tool"] == "dl_add_ytdlp"
      and not worker_calls())
check("no link still asks which video link", dh.tool_dl_add_ytdlp({}).get("needs_clarification") is True
      and "video link" in dh.tool_dl_add_ytdlp({})["message"])

# --------------------------------------------------------------------------- generic
reset_log()
before = len(rows())
page = "http://some-site.example/watch/page-with-a-video"
r = dh.tool_dl_add_ytdlp({"url": page, "generic": True})
check("generic=true while the extractor is switched off: one plain sentence, nothing queued, no trace",
      r.get("ok") is False and r.get("queued") == 0 and "switched off" in r.get("error", "") and "Traceback" not in json.dumps(r)
      and len(r["error"]) < 200 and len(rows()) == before and not worker_calls())
check("...it does not say that generic is available", r.get("generic_available") is False)
r = dh.tool_dl_add_ytdlp({"url": page, "generic": True, "playlist": "all"})
check("generic with playlist=all is refused with a sentence, nothing queued", r.get("ok") is False and len(rows()) == before)
r = dh.tool_dl_add_ytdlp({"url": page})
check("a page no extractor claims, without `generic`: a plain answer, nothing queued",
      r.get("ok") is False and r.get("queued") == 0 and r.get("generic_available") is False and "nothing was queued" in r["message"]
      and len(rows()) == before)

# --------------------------------------------------------------------------- dl_add stays what N4 made it
reset_log()
before = len(rows())
r = dh.tool_dl_add({"url": LIST, "start_paused": True})
check("dl_add with a playlist link is not hooked: one row, the answer names dl_add_ytdlp, no probe",
      r["ok"] and r["count"] == 1 and r["added"][0]["add_tool"] == "dl_add_ytdlp" and not worker_calls()
      and len(rows()) == before + 1)

# --------------------------------------------------------------------------- guests are refused before any look-up
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
looked = []
real_probe = pl.probe_playlist
pl.probe_playlist = lambda *a, **k: (looked.append(a) or real_probe(*a, **k))
reset_log()
before = len(rows())
try:
    for args in ({"url": LIST}, {"url": LIST, "playlist": "all"}, {"url": MIXED, "playlist": "all"}, {"url": ONE},
                 {"url": "http://some-site.example/p", "generic": True}):
        r = dh.tool_dl_add_ytdlp(args)
        check("a chat guest is refused (%s)" % json.dumps(args)[:60],
              r.get("ok") is False and "owner" in r.get("error", "") and "queued" not in r)
    check("...no playlist look-up was made and nothing was queued", not looked and not worker_calls() and len(rows()) == before)
finally:
    pl.probe_playlist = real_probe
    os.environ.pop("JARVIS_CHANNEL_SENDER", None)
r = dh.tool_dl_add_ytdlp({"url": LIST})
check("the owner is asked again once the guest is gone", r.get("needs_clarification") is True)

# --------------------------------------------------------------------------- the checklist entry is current
entry = dh.TEST_CHECKLIST["dl_add_ytdlp"]
text = json.dumps(entry)
check("the Test Checklist no longer says that THIS tool is not hooked",
      "this tool is not hooked" not in text and "no `playlist` or `generic` argument" not in text)
check("...it has a step that sends a playlist link and one that says all",
      any("playlist" in json.dumps(s.get("run") or s.get("ask") or "") for s in entry["steps"])
      and any("all" in json.dumps(s.get("run") or s.get("ask") or "") for s in entry["steps"]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
