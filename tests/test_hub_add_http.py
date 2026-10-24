"""Download Hub, N4 / brief 31: `item_options` on service.add_items and the dl_add_http tool on top of it.

Covers: the allow-list (`segments` only; an unknown option, a bad type, a bool, 0 and 17 are errors that
queue nothing); `segments` is stored on rows the http engine takes and ignored (with a note) on the rest;
a duplicate keeps the options it has and says so; dl_list shows prefs.segments; older callers of
add_items (no item_options) and a fake add_items without the argument keep working; dl_add_http forces
the http engine (a video page / a magnet is refused), takes url / urls / dest / group / start_paused /
dry_run / the per-download extras, validates `segments`, is refused to a chat guest, and is loaded by
the real loader with a checklist entry, keywords, the downloads group and no pack instruction of its own;
dl_add (the router) is unchanged for plain links.

Run: python tests/test_hub_add_http.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-addhttp-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_loader, tools  # noqa: E402
from jarvis.actions import download_add_http as dah  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import service  # noqa: E402
from jarvis.hub.engines import stremio, ytdlp  # noqa: E402

passed = failed = 0
PUBLIC = "http://93.184.216.34"
HASH = "0123456789abcdef0123456789abcdef01234567"
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
    return {i["id"]: i for i in dh.tool_dl_list({})["items"]}


# ---------------------------------------------------------------- the allow-list (service)
norm = service.normalize_engine_item_options
check("nothing is fine", norm(None) == ({}, None) and norm({}) == ({}, None))
check("segments 4 is kept as a whole number", norm({"segments": 4}) == ({"segments": 4}, None))
check("segments as text '8' is accepted", norm({"segments": "8"})[0] == {"segments": 8})
check("segments None leaves it unset", norm({"segments": None}) == ({}, None))
for bad in (0, 17, -1, "many", True, 2.5, [4]):
    out, problem = norm({"segments": bad})
    check("segments %r is refused" % (bad,), out == {} and bool(problem))
out, problem = norm({"headers": {"Authorization": "Bearer x"}})
check("headers are not an item option (an unknown key is an error naming the known ones)",
      out == {} and "headers" in problem and "segments" in problem)
check("a non-object is refused", bool(norm("segments=4")[1]) and bool(norm([1])[1]))

# ---------------------------------------------------------------- service.add_items
r = service.add_items([PUBLIC + "/a.iso"], start_paused=True, item_options={"segments": 6})
row = rows()[r["added"][0]["id"]]
check("add_items stores segments on an http row", r["ok"] and row["engine"] == "http" and row["prefs"].get("segments") == 6)
check("...and nothing in the answer says it was ignored", "ignored" not in r.get("note", ""))

r = service.add_items([PUBLIC + "/b.iso"], start_paused=True)
check("without item_options nothing is stored (old callers)", "segments" not in rows()[r["added"][0]["id"]]["prefs"])

r = service.add_items([PUBLIC + "/a.iso"], start_paused=True, item_options={"segments": 2})
check("a duplicate is reported, keeps its options and the note says so",
      r["added"][0]["duplicate"] is True and "not changed" in r.get("note", "")
      and rows()[r["added"][0]["id"]]["prefs"].get("segments") == 6)

before = len(rows())
r = service.add_items([PUBLIC + "/c.iso"], start_paused=True, item_options={"segments": 99})
check("a bad value queues nothing", r["ok"] is False and "segments" in r["error"] and len(rows()) == before)
r = service.add_items([PUBLIC + "/c.iso"], start_paused=True, item_options={"colour": "red"})
check("an unknown option queues nothing", r["ok"] is False and "colour" in r["error"] and len(rows()) == before)

r = service.add_items([YT, PUBLIC + "/d.iso"], start_paused=True, item_options={"segments": 3})
by_engine = {a["engine"]: rows()[a["id"]] for a in r["added"]}
check("a video row ignores segments, the file row keeps it",
      "segments" not in by_engine["ytdlp"]["prefs"] and by_engine["http"]["prefs"].get("segments") == 3)
check("...and the note says the other links ignored it", "ignored" in r.get("note", "") and "http" in r["note"])

# ---------------------------------------------------------------- dl_add_http
sch = next(s for s in dah.TOOL_SCHEMAS if s["name"] == "dl_add_http")
check("the schema offers segments 1..16 and the shared few",
      set(sch["parameters"]["properties"]) == {"url", "urls", "segments", "dest", "group", "start_paused", "start_at", "dry_run"}
      and sch["parameters"]["properties"]["segments"]["maximum"] == 16)
check("the schema is small", len(json.dumps(sch)) <= 2200)
check("it has no engine argument (it is the engine)", "engine" not in sch["parameters"]["properties"])

r = dah.tool_dl_add_http({"url": PUBLIC + "/e.iso", "segments": 4, "start_paused": True})
row = rows()[r["added"][0]["id"]]
check("dl_add_http queues on the http engine with segments", r["ok"] and row["engine"] == "http" and row["prefs"].get("segments") == 4)
r = dah.tool_dl_add_http({"url": PUBLIC + "/f.iso", "start_paused": True})
check("without segments it just queues (the person's own http.segments applies)",
      r["ok"] and "segments" not in rows()[r["added"][0]["id"]]["prefs"])
r = dah.tool_dl_add_http({"urls": [PUBLIC + "/g1.iso", PUBLIC + "/g2.iso"], "group": "Two", "segments": 2, "start_paused": True})
check("several links share the options and go in one group", r["ok"] and r["count"] == 2 and r["group"]
      and all(rows()[a["id"]]["prefs"].get("segments") == 2 for a in r["added"]))
r = dah.tool_dl_add_http({"url": PUBLIC + "/h.iso", "segments": 1, "start_paused": True, "notify": "off", "speed_limit_kbps": 50})
check("segments 1 is stored (one connection for this download) and the extras are accepted",
      r["ok"] and rows()[r["added"][0]["id"]]["prefs"].get("segments") == 1)

n = len(rows())
for label, args in (("99", {"url": PUBLIC + "/i.iso", "segments": 99}),
                    ("text", {"url": PUBLIC + "/i.iso", "segments": "lots"}),
                    ("a bool", {"url": PUBLIC + "/i.iso", "segments": True})):
    r = dah.tool_dl_add_http(args)
    check("segments %s is refused and nothing is queued" % label, r.get("ok") is False and "segments" in r["error"] and len(rows()) == n)

r = dah.tool_dl_add_http({"url": YT, "segments": 4})
check("a video page is refused (it belongs to dl_add_ytdlp)", r.get("ok") is False and r.get("skipped") and len(rows()) == n)
r = dah.tool_dl_add_http({"url": "magnet:?xt=urn:btih:" + HASH})
check("a magnet is refused (it belongs to dl_add_stremio)", r.get("ok") is False and len(rows()) == n)
check("no link asks which", dah.tool_dl_add_http({}).get("needs_clarification") is True)

r = dah.tool_dl_add_http({"url": PUBLIC + "/j.iso", "segments": 4, "dry_run": True})
check("dry_run names the engine, echoes the option and queues nothing",
      r["ok"] and r["dry_run"] and r["decisions"][0]["engine"] == "http" and r.get("item_options") == {"segments": 4} and len(rows()) == n)

r = dh.tool_dl_add({"url": PUBLIC + "/k.iso", "start_paused": True})
check("dl_add (the router) still queues a plain link as before, with no segments",
      r["ok"] and r["added"][0]["engine"] == "http" and r["added"][0].get("add_tool") == "dl_add"
      and "segments" not in rows()[r["added"][0]["id"]]["prefs"])

# an older add_items (a fake without the argument) is still called exactly as before
seen = {}
real = service.add_items


def fake(urls, **kw):
    seen.update(kw)
    return {"ok": True, "added": [], "count": 0}


service.add_items = fake
dh.tool_dl_add({"url": PUBLIC + "/l.iso"})
check("dl_add never passes item_options to add_items", "item_options" not in seen)
seen.clear()
dah.tool_dl_add_http({"url": PUBLIC + "/l.iso", "segments": 5})
check("dl_add_http passes it on", seen.get("item_options") == {"segments": 5})
service.add_items = real

# ---------------------------------------------------------------- guest
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
n = len(rows())
r = dah.tool_dl_add_http({"url": PUBLIC + "/m.iso", "segments": 4})
check("a chat guest is refused and nothing is queued", r.get("ok") is False and len(rows()) == n)
check("a guest is not even offered it (dl_add_* names are hidden)", "dl_add_http" in tools.hidden_from_sender())
os.environ.pop("JARVIS_CHANNEL_SENDER")

# ---------------------------------------------------------------- the real loader
check("the real loader has it in the tool table and the downloads group",
      "dl_add_http" in tools.TOOLS and tool_loader.tool_group("dl_add_http") == "downloads"
      if hasattr(tool_loader, "tool_group") else "dl_add_http" in tools.TOOLS)
check("it has a checklist entry and keywords", "dl_add_http" in dah.TEST_CHECKLIST and dah.TOOL_KEYWORDS["dl_add_http"])
check("none of dl_add's or ytdl_download's phrases is taken",
      not (set(dah.TOOL_KEYWORDS["dl_add_http"]) & (set(dh.TOOL_KEYWORDS["dl_add"]) | {"download video", "download youtube", "download this video"})))
check("it declares no pack instruction (the group has one, in download_hub.py)", not hasattr(dah, "TOOL_PACK_INSTRUCTION"))
check("it is not confirm-gated by name (its questions are asked inside)", "dl_add_http" not in dh.TOOL_CONFIRM_REQUIRED)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
