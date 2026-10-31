"""Download Hub, N4: dl_add is split into a router and per-source add tools.

Covers: dl_add's schema is the shared core only (about a quarter of what it was) and carries no
yt-dlp option; dl_add_ytdlp holds the 14 yt-dlp options, dl_add_stremio the magnet / stream /
file choice; the per-download extras are accepted by every add tool but not advertised; dl_add's
dry run and its added rows name the add tool for the engine; the old yt-dlp arguments on dl_add
still work and say they are deprecated; dl_add_ytdlp forces the yt-dlp engine and validates its
options; dl_add_stremio builds one canonical magnet (infohash, `file_index`, a stream object, a
.torrent link, bad input); the bulk question and a chat guest's refusal are shared by all three;
dl_providers shows each provider's add tool.

And the generator (tool_download_add.py through the real loader): an `add` block in
TOOL_DOWNLOADS makes `dl_add_<id>` with no other edit, it goes away with the file, the module's
own TOOLS is never touched, it validates its arguments, supports dry_run, runs the file's own
tool through execute_tool, inherits that tool's confirm gate, is refused to chat guests, has a
checklist entry and keywords, never replaces a hand-written tool of the same name, and a bad
block is dropped without taking the file or the provider down.

Run: python tests/test_hub_add_tools.py
"""
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-add-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_download_add, tool_loader, tools  # noqa: E402
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


ytdlp.ENGINE.available = lambda: (True, "")          # no yt-dlp / ffmpeg needed here
stremio.ENGINE.available = lambda: (True, "")        # nor a Stremio server


def schema(name):
    return next(s for s in dh.TOOL_SCHEMAS if s["name"] == name)


def props(name):
    return set(schema(name)["parameters"]["properties"])


# ---------------------------------------------------------------- the schemas
CORE = {"url", "urls", "engine", "dest", "group", "start_paused", "start_at", "dry_run"}
EXTRAS = set(dh._EXTRA_ARGS)
VIDEO = set(dh._VIDEO_ARGS)

check("dl_add keeps only the shared core", props("dl_add") == CORE)
check("dl_add carries no yt-dlp option", not (props("dl_add") & VIDEO))
check("dl_add is about a quarter of its old size (it was 5,414 characters)", len(json.dumps(schema("dl_add"))) <= 1600)
check("dl_add still tells the model about ytdl_download and dry_run",
      "ytdl_download" in schema("dl_add")["description"] and "dry_run" in schema("dl_add")["description"])
check("dl_add names both add tools", "dl_add_ytdlp" in schema("dl_add")["description"] and "dl_add_stremio" in schema("dl_add")["description"])
check("dl_add_ytdlp: url / urls, the shared few and the 14 yt-dlp options",
      # `playlist` and `generic` (brief 21) are the yt-dlp tool's own two extras; they are optional here
      # so this check holds both before and after they are added to the schema.
      props("dl_add_ytdlp") - {"playlist", "generic"} == {"url", "urls", "dest", "group", "start_paused", "start_at", "dry_run"} | VIDEO
      and len(VIDEO) == 14)
check("dl_add_ytdlp has no engine argument (it is the engine)", "engine" not in props("dl_add_ytdlp"))
check("dl_add_stremio: magnet / stream / file_index and the shared few",
      props("dl_add_stremio") == {"magnet", "stream", "file_index", "dest", "group", "start_paused", "start_at", "dry_run"})
check("dl_add_stremio is small", len(json.dumps(schema("dl_add_stremio"))) <= 1800)
check("the per-download extras are in no schema by default",
      not any(props(n) & EXTRAS for n in ("dl_add", "dl_add_ytdlp", "dl_add_stremio")) and dh.ADVERTISE_PER_DOWNLOAD_EXTRAS is False)
dh.ADVERTISE_PER_DOWNLOAD_EXTRAS = True
check("...and one switch puts them back in all three", all(set(dh._add_props(n)) >= EXTRAS for n in (("url",), ("magnet",), ("dest",))))
dh.ADVERTISE_PER_DOWNLOAD_EXTRAS = False
check("the video-only words stay out of dl_add's own description",
      "mode / quality" not in schema("dl_add")["description"])
check("a model searching 'queue a video 720p mp4 audio' finds the yt-dlp tool by its description",
      all(w in schema("dl_add_ytdlp")["description"].lower() for w in ("720p", "mp4", "audio only", "subtitles")))

# ---------------------------------------------------------------- keywords leave ytdl_download its phrases
kw = dh.TOOL_KEYWORDS
check("the add tools have phrases of their own", bool(kw.get("dl_add_ytdlp")) and bool(kw.get("dl_add_stremio")))
check("none of ytdl_download's phrases is taken by an add tool",
      not ({"download video", "download youtube", "download this video"} & (set(kw["dl_add_ytdlp"]) | set(kw["dl_add_stremio"]) | set(kw["dl_add"]))))
check("the pack instruction says where video options go", "dl_add_ytdlp" in dh.TOOL_PACK_INSTRUCTION and "dl_add_stremio" in dh.TOOL_PACK_INSTRUCTION)
check("the add tools are not confirm-gated by name (their questions are asked inside)",
      not ({"dl_add", "dl_add_ytdlp", "dl_add_stremio"} & dh.TOOL_CONFIRM_REQUIRED))

# ---------------------------------------------------------------- dl_add, the router
r = dh.tool_dl_add({"urls": [PUBLIC + "/a.zip", YT, "magnet:?xt=urn:btih:" + HASH], "dry_run": True})
tools_named = [d["add_tool"] for d in r["decisions"]]
check("dry run: a plain link says dl_add, a video page dl_add_ytdlp, a magnet dl_add_stremio",
      tools_named == ["dl_add", "dl_add_ytdlp", "dl_add_stremio"])
check("dry run: the engine and the reason are still there", [d["engine"] for d in r["decisions"]] == ["http", "ytdlp", "stremio"]
      and all(d["reason"] for d in r["decisions"]))

r = dh.tool_dl_add({"url": PUBLIC + "/f1.bin", "start_paused": True})
check("an added row names its add tool", r["ok"] and r["added"][0]["engine"] == "http" and r["added"][0]["add_tool"] == "dl_add")
r = dh.tool_dl_add({"url": YT, "start_paused": True})
check("a video page added through dl_add names dl_add_ytdlp", r["ok"] and r["added"][0]["add_tool"] == "dl_add_ytdlp")
check("...with no deprecation note when no video option was sent", "note" not in r or "deprecated" not in r.get("note", ""))
r = dh.tool_dl_add({"url": "https://youtu.be/bbbbbbbbbbb", "mode": "audio", "start_paused": True})
rows = {i["id"]: i for i in dh.tool_dl_list({})["items"]}
check("the old yt-dlp arguments on dl_add still work (forwarded)", r["ok"] and rows[r["added"][0]["id"]]["kind"] == "audio")
check("...and the answer says they are deprecated", "deprecated" in r.get("note", "") and "dl_add_ytdlp" in r["note"])
r = dh.tool_dl_add({"url": PUBLIC + "/f2.bin", "start_paused": True, "notify": "off", "speed_limit_kbps": 50})
check("the per-download extras still work on dl_add", r["ok"] and r["count"] == 1)
check("dl_add with no link asks which", dh.tool_dl_add({}).get("needs_clarification") is True)

# ---------------------------------------------------------------- dl_add_ytdlp
r = dh.tool_dl_add_ytdlp({"url": YT, "mode": "audio", "audio_format": "flac", "start_paused": True})
rows = {i["id"]: i for i in dh.tool_dl_list({})["items"]}
check("dl_add_ytdlp queues on the yt-dlp engine with the options",
      r["ok"] and r["added"][0]["engine"] == "ytdlp" and rows[r["added"][0]["id"]]["kind"] == "audio" and r["added"][0]["add_tool"] == "dl_add_ytdlp")
r = dh.tool_dl_add_ytdlp({"urls": ["https://youtu.be/ccccccccccc", "https://youtu.be/ddddddddddd"], "group": "Two", "start_paused": True})
check("several links with the same options go in one group", r["ok"] and r["count"] == 2 and r["group"])
r = dh.tool_dl_add_ytdlp({"url": YT, "container": "gif"})
check("a bad option is refused and nothing is queued", r.get("ok") is False and "container" in r["error"])
r = dh.tool_dl_add_ytdlp({"url": PUBLIC + "/plain.zip"})
check("a plain file link is refused by the yt-dlp tool", r.get("ok") is False and r["skipped"] and "can't take" in r["skipped"][0]["problem"])
check("no link asks which", dh.tool_dl_add_ytdlp({}).get("needs_clarification") is True)
r = dh.tool_dl_add_ytdlp({"url": YT, "dry_run": True})
check("its dry run forces the engine and names the tool", r["decisions"][0]["engine"] == "ytdlp" and r["decisions"][0]["add_tool"] == "dl_add_ytdlp")
r = dh.tool_dl_add_ytdlp({"url": YT, "start_paused": True, "notify": "off"})
check("the extras are accepted here too", r["ok"])

# ---------------------------------------------------------------- dl_add_stremio
r = dh.tool_dl_add_stremio({"magnet": HASH, "file_index": 2, "dry_run": True})
check("an infohash plus file_index becomes a magnet with so=2", r["decisions"][0]["input"].endswith("&so=2")
      and r["decisions"][0]["engine"] == "stremio" and r["decisions"][0]["add_tool"] == "dl_add_stremio")
r = dh.tool_dl_add_stremio({"magnet": "magnet:?xt=urn:btih:%s&so=1" % HASH, "file_index": 4, "dry_run": True})
check("file_index wins over a so= already in the magnet", r["decisions"][0]["input"].endswith("&so=4") and "&so=1" not in r["decisions"][0]["input"])
r = dh.tool_dl_add_stremio({"stream": {"infoHash": HASH, "fileIdx": 5}, "dry_run": True})
check("a stream object's fileIdx carries through", r["decisions"][0]["input"].endswith("&so=5"))
r = dh.tool_dl_add_stremio({"stream": {"infoHash": HASH, "fileIdx": 5}, "file_index": 0, "dry_run": True})
check("file_index 0 is a real choice and overrides the stream's", r["decisions"][0]["input"].endswith("&so=0"))
r = dh.tool_dl_add_stremio({"magnet": "magnet:?xt=urn:btih:" + HASH, "start_paused": True})
check("a magnet is queued on the Stremio engine", r["ok"] and r["added"][0]["engine"] == "stremio" and r["added"][0]["add_tool"] == "dl_add_stremio")
check("a .torrent link is passed through unchanged",
      dh.tool_dl_add_stremio({"magnet": PUBLIC + "/x.torrent", "dry_run": True})["decisions"][0]["input"] == PUBLIC + "/x.torrent")
check("a .torrent link can't carry a file choice", "file_index" in dh.tool_dl_add_stremio({"magnet": PUBLIC + "/x.torrent", "file_index": 1})["error"])
check("not a magnet is refused in plain words", "not a magnet" in dh.tool_dl_add_stremio({"magnet": "hello"})["error"])
check("a magnet and a stream together are refused", "not both" in dh.tool_dl_add_stremio({"magnet": HASH, "stream": {"infoHash": HASH}})["error"])
check("a stream without an infoHash points at the right tool", "dl_add_ytdlp" in dh.tool_dl_add_stremio({"stream": {"ytId": "abcdefghijk"}})["error"])
check("a bad file_index is refused", all(dh.tool_dl_add_stremio({"magnet": HASH, "file_index": bad}).get("ok") is False for bad in (-1, "x", True, 10 ** 9)))
check("nothing given asks which", dh.tool_dl_add_stremio({}).get("needs_clarification") is True)

# ---------------------------------------------------------------- shared rules
asked = []
import jarvis.ui_bridge as ui  # noqa: E402
_real_confirm = ui.confirm
ui.confirm = lambda *a, **k: (asked.append(a[0]) or False)
many = ["https://youtu.be/%011d" % i for i in range(8)]
r = dh.tool_dl_add_ytdlp({"urls": many})
check("the bulk question is asked by the yt-dlp tool too (default no: nothing queued)", asked and r.get("cancelled") is True)
asked.clear()
r = dh.tool_dl_add({"urls": [PUBLIC + "/m%d.bin" % i for i in range(8)]})
check("...and by dl_add, as before", asked and r.get("cancelled") is True)
asked.clear()
r = dh.tool_dl_add_stremio({"magnet": HASH, "dest": "D:/Games"})
check("a full path outside the library asks on the Stremio tool", asked and r.get("cancelled") is True)
ui.confirm = _real_confirm

os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
guest = [dh.tool_dl_add({"url": PUBLIC + "/g.bin"}), dh.tool_dl_add_ytdlp({"url": YT}), dh.tool_dl_add_stremio({"magnet": HASH})]
check("a chat guest is refused by all three add tools", all(g.get("ok") is False and "owner" in g["error"] for g in guest))
check("...and can still look", dh.tool_dl_list({}).get("ok") is True)
os.environ["JARVIS_CHANNEL_SENDER"] = "{not json"
check("an unreadable sender counts as a guest (fails closed)", dh.tool_dl_add({"url": PUBLIC + "/g.bin"}).get("ok") is False)
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
check("the owner on a chat platform is let through", dh.tool_dl_add({"url": PUBLIC + "/o1.bin", "start_paused": True}).get("ok") is True)
os.environ.pop("JARVIS_CHANNEL_SENDER")
check("no chat context (the PC, a scheduled job) is the owner's own", dh.tool_dl_add({"url": PUBLIC + "/o2.bin", "start_paused": True}).get("ok") is True)

# ---------------------------------------------------------------- dl_providers shows the add tool
res = dh.tool_dl_providers({})
check("dl_providers lists providers with an add_tool field (none: no loaded tool of that name)",
      res.get("ok") is not False and all("add_tool" in p and p["add_tool"] is None for p in res.get("providers", [])))

# ---------------------------------------------------------------- the generator, through the real loader
PROVIDER_FILE = textwrap.dedent('''
    CALLS = []
    TOOL_GROUP = "fakefetch"
    TOOL_SCHEMAS = [{"name": "fake_start", "description": "Start a fake fetch.",
                     "parameters": {"type": "object", "properties": {}}}]
    def tool_fake_start(args):
        CALLS.append(dict(args))
        return {"ok": True, "started": args.get("repo")}
    TOOLS = {"fake_start": tool_fake_start}
    TOOL_CONFIRM_REQUIRED = {"fake_start"}
    TOOL_DOWNLOADS = [{
        "id": "model_fetch", "label": "Model downloads", "mode": "push",
        "add": {
            "tool": "fake_start",
            "description": "Fetch a model from the hub",
            "params": {
                "repo": {"type": "string", "description": "owner/name"},
                "size_gb": {"type": "integer", "minimum": 1, "maximum": 500},
                "precision": {"type": "string", "enum": ["fp16", "int8"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["repo"],
            "shared": ["dest"],
        },
    }]
''')


def make_dir(files):
    d = tempfile.mkdtemp(prefix="jarvis-actions-")
    for name, body in files.items():
        Path(d, name).write_text(body, encoding="utf-8")
    return d


def discover(files, reserved=None, directory=None):
    """Run the real discovery on a temp actions folder.  The loader reuses a module it has already
    imported, so each call starts without the test modules unless the same folder is passed again."""
    log = []
    if directory is None:
        for name in files:
            sys.modules.pop("jarvis.actions." + Path(name).stem, None)
        directory = make_dir(files)
    recs = tool_loader.discover_actions(Path(directory), set(reserved or ()), log.append)
    return {r.file: r for r in recs}, log, directory


recs, log, _dir = discover({"fake_provider.py": PROVIDER_FILE})
rec = recs["fake_provider.py"]
gen = "dl_add_model_fetch"
check("the file loads and the generated tool appears with no other edit", rec.valid and gen in rec.tools and "fake_start" in rec.tools)
sch = next(s for s in rec.schemas if s["name"] == gen)
check("its schema is made from the block (params, shared dest, dry_run, required)",
      set(sch["parameters"]["properties"]) == {"repo", "size_gb", "precision", "tags", "dest", "dry_run"} and sch["parameters"]["required"] == ["repo"])
check("its description is the block's line plus where it goes", sch["description"].startswith("Fetch a model from the hub.") and "Model downloads" in sch["description"])
check("the provider entry still loads (the `add` key did not break it)", [d["id"] for d in rec.downloads] == ["model_fetch"] and "add" not in rec.downloads[0])
check("it is confirm-gated because fake_start is", gen in rec.confirm_required and "fake_start" in rec.confirm_required)
check("it has a checklist entry in the file's group", gen in rec.checklist and rec.checklist[gen].get("group") == "fakefetch" and rec.checklist[gen]["steps"])
check("it has its own routing phrases", gen in rec.keywords and any("model downloads" in k for k in rec.keywords[gen]))
mod = sys.modules["jarvis.actions.fake_provider"]
check("the module's own TOOLS and TOOL_SCHEMAS were not touched", gen not in mod.TOOLS and len(mod.TOOL_SCHEMAS) == 1)
recs2, _, _ = discover({}, directory=_dir)
check("discovering again does not add it twice", sum(1 for s in recs2["fake_provider.py"].schemas if s["name"] == gen) == 1)
recs3, _, _ = discover({})
check("without the file there is no dl_add_model_fetch", not recs3)
unrelated, _, _ = discover({"plain.py": 'TOOL_GROUP = "plain"\nTOOL_SCHEMAS = [{"name": "plain_one", "description": "x", "parameters": {"type": "object", "properties": {}}}]\nTOOLS = {"plain_one": lambda a: {}}\n'})
check("a file with no add block gains nothing", sorted(unrelated["plain.py"].tools) == ["plain_one"])

handler = rec.tools[gen]
check("a missing required argument is named", "'repo' is required" in handler({})["error"])
check("an unknown argument is named with the known ones", "unknown argument 'bogus'" in handler({"repo": "a/b", "bogus": 1})["error"]
      and "repo" in handler({"repo": "a/b", "bogus": 1})["error"])
check("a wrong type is refused", "'size_gb' must be an integer" in handler({"repo": "a/b", "size_gb": "big"})["error"])
check("a value outside the enum is refused", "fp16" in handler({"repo": "a/b", "precision": "fp4"})["error"])
check("a number outside its range is refused", "at most 500" in handler({"repo": "a/b", "size_gb": 900})["error"])
check("an array of the wrong item type is refused", handler({"repo": "a/b", "tags": [1, {}]}).get("ok") is False)
check("a numeric string is accepted for an integer", handler({"repo": "a/b", "size_gb": "12", "dry_run": True})["arguments"]["size_gb"] == 12)
dry = handler({"repo": "a/b", "precision": "int8", "dest": "Models", "dry_run": True})
check("dry_run checks and says what would run, and runs nothing",
      dry["ok"] and dry["dry_run"] and dry["would_run"] == "fake_start" and dry["arguments"] == {"repo": "a/b", "precision": "int8", "dest": "Models"}
      and mod.CALLS == [])

_ran = []
_real_execute = tools.execute_tool
tools.execute_tool = lambda name, args=None, **kw: (_ran.append((name, dict(args or {}))) or {"ok": True, "queued": 1})
out = handler({"repo": "a/b", "size_gb": 3})
check("a real call runs the file's own tool through execute_tool with the checked arguments",
      _ran == [("fake_start", {"repo": "a/b", "size_gb": 3})] and out["ok"] and out["add_tool"] == gen and out["provider"] == "model_fetch")
tools.execute_tool = _real_execute

os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "instagram", "user_id": "7", "is_owner": False})
_ran.clear()
tools.execute_tool = lambda name, args=None, **kw: (_ran.append(name) or {"ok": True})
g = handler({"repo": "a/b"})
check("a chat guest is refused by a generated tool and nothing runs", g.get("ok") is False and "owner" in g["error"] and not _ran)
tools.execute_tool = _real_execute
os.environ.pop("JARVIS_CHANNEL_SENDER")

# name clashes: a hand-written tool of the same name always wins
HAND = ('TOOL_GROUP = "hand"\nTOOL_SCHEMAS = [{"name": "dl_add_model_fetch", "description": "By hand.", '
        '"parameters": {"type": "object", "properties": {}}}]\nTOOLS = {"dl_add_model_fetch": lambda a: {"hand": True}}\n')
recs, log, _dir = discover({"a_hand.py": HAND, "fake_provider.py": PROVIDER_FILE})
check("a hand-written tool of that name wins, whichever file comes first", "dl_add_model_fetch" in recs["a_hand.py"].tools
      and recs["a_hand.py"].valid and gen not in recs["fake_provider.py"].tools and recs["fake_provider.py"].valid)
check("...and the clash is logged", any("already exists" in line for line in log))
recs, log, _dir = discover({"z_hand.py": HAND, "fake_provider.py": PROVIDER_FILE})
check("...also when the hand-written file sorts after the provider", recs["z_hand.py"].valid and gen not in recs["fake_provider.py"].tools)
recs, log, _dir = discover({"fake_provider.py": PROVIDER_FILE}, reserved={gen})
check("a reserved (built-in) name is never taken", gen not in recs["fake_provider.py"].tools and recs["fake_provider.py"].valid)

# bad blocks: logged and dropped, the file and the provider load regardless
def provider_with(add_src):
    return PROVIDER_FILE.split("    TOOL_DOWNLOADS")[0] + (
        'TOOL_DOWNLOADS = [{"id": "model_fetch", "label": "Model downloads", "mode": "push", "add": %s}]\n' % add_src)


BAD = {
    "a tool that is not the file's": '{"tool": "nope", "description": "x"}',
    "a shared name that does not exist": '{"tool": "fake_start", "description": "x", "params": {}, "shared": ["bogus"]}',
    "an unknown key": '{"tool": "fake_start", "description": "x", "colour": 1}',
    "no description": '{"tool": "fake_start"}',
    "a description over 200 characters": '{"tool": "fake_start", "description": "%s"}' % ("x" * 201),
    "dry_run in params": '{"tool": "fake_start", "description": "x", "params": {"dry_run": {"type": "boolean"}}}',
    "a bad param name": '{"tool": "fake_start", "description": "x", "params": {"Bad-Name": {"type": "string"}}}',
    "a bad param type": '{"tool": "fake_start", "description": "x", "params": {"a": {"type": "float"}}}',
    "required naming a missing param": '{"tool": "fake_start", "description": "x", "params": {}, "required": ["ghost"]}',
    "more than 12 arguments": '{"tool": "fake_start", "description": "x", "params": {%s}}' % ", ".join('"p%d": {"type": "string"}' % i for i in range(12)),
    "an oversize schema": '{"tool": "fake_start", "description": "x", "params": {%s}}' % ", ".join('"p%d": {"type": "string", "description": "%s"}' % (i, "y" * 290) for i in range(8)),
    "a block that is not a dict": '"nope"',
}
for label, src in BAD.items():
    recs, log, _dir = discover({"fake_provider.py": provider_with(src)})
    r_ = recs["fake_provider.py"]
    check("bad block (%s): dropped and logged, the file and provider still load" % label,
          r_.valid and gen not in r_.tools and "fake_start" in r_.tools and [d["id"] for d in r_.downloads] == ["model_fetch"]
          and any("[tool-downloads]" in line for line in log))
recs, log, _dir = discover({"fake_provider.py": provider_with('{"tool": "fake_start", "description": "Fetch it."}')})
check("a block with only a tool and a description is fine (no params: just dry_run)",
      set(next(s for s in recs["fake_provider.py"].schemas if s["name"] == gen)["parameters"]["properties"]) == {"dry_run"})

check("the real loader result for the shipped files is unchanged: no generated tool without an add block",
      not [n for n in tools.TOOLS if n.startswith("dl_add_") and n not in ("dl_add_ytdlp", "dl_add_stremio", "dl_add_http", "dl_add_hoster")])
check("the shipped add tools are live in the tool table and in the downloads group",
      all(n in tools.TOOLS for n in ("dl_add", "dl_add_ytdlp", "dl_add_stremio")))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
