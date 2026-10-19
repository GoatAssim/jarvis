"""Download Hub phases 4 and 7: the model-facing tools against a fake Stremio server.

stack_status / stack_start / stack_stop (an already-running server is "already up", never started
twice, never killed), stack_doctor (and that it never prints the account key), stremio_prepare (the
read-modify-write round trip, verification, the disk check, input validation), server_cache,
server_cache_clear (refusals), stremio_open, play_episode (three tiers, the fallbacks between them,
what is refused), play_local (only finished files under the library), the palette commands and the
tool-file declarations (the daemons, confirm gating, checklist entries).

Run: python tests/test_hub_stremio_tools.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stremio-tools-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_stremio as fake  # noqa: E402
from jarvis import ui_bridge, tool_commands, checklist_schema, tool_daemons  # noqa: E402
from jarvis.actions import play_media, stremio_open, stremio_stack  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import config, player, server as sserver  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


FAKE, BASE, ST = fake.start()
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
H1 = "11" * 20
CACHE = Path(HOME) / "cache"
LIB = Path(HOME) / "lib"
cfg_path = config.config_path()
cfg_path.parent.mkdir(parents=True, exist_ok=True)
# stremio-web "is up" = the fake answers on the configured web port (a loopback port)
cfg_path.write_text(json.dumps({"web": {"url": BASE}, "account": {"auth_key": "SECRETKEY"}}))
ST.torrents[H1] = {"name": "Sintel", "files": [("Sintel/Sintel.mp4", 3000)]}
ST.settings["cacheRoot"] = str(CACHE)

# ---- stack_status / start / stop
st = stremio_stack.tool_stack_status({})
check("stack_status: a server the app started reads 'already up'", st["ok"] and st["server"]["up"] and "already up" in st["server"]["note"]
      and st["server"]["version"] == "4.21.2" and not st["server"]["managed_by_jarvis"])
check("stack_status summary is one line", "already up" in st["summary"] and "\n" not in st["summary"])
started = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start with the server answering starts nothing", started["ok"] and started["server"]["state"] == "already_up")
check("stack_start rejects a bad target", not stremio_stack.tool_stack_start({"what": "everything"})["ok"])
stopped = stremio_stack.tool_stack_stop({"what": "server"})
check("stack_stop never kills a server it didn't start", stopped["ok"] and stopped["server"]["state"] == "not_ours")
doc = stremio_stack.tool_stack_doctor({})
names = {c["name"]: c for c in doc["checks"]}
check("stack_doctor reports the server and its version", names["server"]["status"] == "ok" and "4.21.2" in names["server"]["detail"])
check("stack_doctor flags a finite cache", names["cache"]["status"] == "warn")
check("stack_doctor never prints the account key", "SECRETKEY" not in json.dumps(doc))

# ---- stremio_prepare
asked = []
res = stremio_stack.tool_stremio_prepare({})
check("stremio_prepare defaults to an unlimited cache and verifies it",
      res["ok"] and res["changed"] == {"cacheSize": [2147483648, None]} and ST.settings["cacheSize"] is None)
check("the whole settings object was posted back (nothing blanked)", ST.settings["serverVersion"] == "4.21.2"
      and ST.settings["btMaxConnections"] == 55)
check("a second run changes nothing", stremio_stack.tool_stremio_prepare({})["changed"] == {})
res = stremio_stack.tool_stremio_prepare({"max_connections": 120, "speed_limit_mbps": 4, "cache": "keep"})
check("connections and speed limit are set (hard limit 1.4x soft)", res["ok"] and ST.settings["btMaxConnections"] == 120
      and ST.settings["btDownloadSpeedSoftLimit"] == 4 * 1024 * 1024 and ST.settings["btDownloadSpeedHardLimit"] == int(4 * 1024 * 1024 * 1.4))
ST.refuse_settings = True
res = stremio_stack.tool_stremio_prepare({"max_connections": 200})
check("a server that doesn't keep a setting is reported, not trusted", not res["ok"] and "btMaxConnections" in res["error"])
ST.refuse_settings = False
for bad in ({"cache_root": "relative/dir"}, {"cache_root": "/a/../b"}, {"cache_root": "/"}, {"max_connections": 5},
            {"max_connections": "lots"}, {"speed_limit_mbps": 0}, {"cache": "-3"}):
    check("stremio_prepare refuses %s" % json.dumps(bad), not stremio_stack.tool_stremio_prepare(bad)["ok"])
res = stremio_stack.tool_stremio_prepare({"cache": "99999999"})
check("a finite cache bigger than the drive's free space is refused", not res["ok"] and ("range" in res["error"] or "free" in res["error"]
      or "between" in res["error"]))
root = str(Path(HOME) / "newcache")
res = stremio_stack.tool_stremio_prepare({"cache_root": root, "cache": "keep"})
check("a new cache folder is created and set", res["ok"] and ST.settings["cacheRoot"] == root and Path(root).is_dir())
ST.settings["cacheRoot"] = str(CACHE)

# ---- server_cache / server_cache_clear
(CACHE / "stremio-cache" / "sub").mkdir(parents=True)
(CACHE / "stremio-cache" / "sub" / "a.bin").write_bytes(b"x" * 1000)
(CACHE / "stremio-cache" / "b.bin").write_bytes(b"y" * 500)
(CACHE / "keep.txt").write_text("not the cache")
ST.registered.append(H1)
res = stremio_stack.tool_server_cache({})
check("server_cache shows folder, size on disk and the torrents", res["ok"] and res["on_disk"]["files"] == 2
      and res["torrents"] and res["torrents"][0]["name"] == "Sintel")
res = stremio_stack.tool_server_cache_clear({})
check("clearing is refused while something streams from the server", not res["ok"] and "streaming" in res["error"]
      and (CACHE / "stremio-cache" / "b.bin").exists())
ST.torrents[H1]["speed"] = 0
res = stremio_stack.tool_server_cache_clear({})
check("clearing is refused when the server wasn't started by Jarvis (and deletes nothing)",
      not res["ok"] and "Stremio app" in res["error"] and (CACHE / "stremio-cache" / "b.bin").exists())
a_link = CACHE / "stremio-cache" / "evil"
try:
    os.symlink(str(Path(HOME)), str(a_link))
    linked = True
except (OSError, NotImplementedError):
    linked = False
files, size, errors = stremio_stack._empty_folder(CACHE / "stremio-cache")
check("emptying removes the contents but not the folder, and never follows a link",
      (CACHE / "stremio-cache").is_dir() and not any((CACHE / "stremio-cache").iterdir()) and (CACHE / "keep.txt").exists()
      and files == 2 and size == 1500 and not errors and Path(HOME).exists())
ST.settings["cacheRoot"] = str(CACHE / "elsewhere")
check("with no cache folder yet there's nothing to clear", stremio_stack.tool_server_cache_clear({}).get("cleared") == 0)
ST.settings["cacheRoot"] = str(CACHE)

# ---- stremio_open
res = stremio_open.tool_stremio_open({"imdb_id": "tt0944947", "season": 1, "episode": 2, "open": False})
check("stremio_open builds the episode link without opening it", res["ok"] and res["link"] == BASE + "/#/detail/series/tt0944947/tt0944947:1:2" and not res["opened"])
opened = []
import webbrowser  # noqa: E402
webbrowser.open = lambda url: (opened.append(url) or True)
res = stremio_open.tool_stremio_open({"screen": "search", "query": "sintel"})
check("stremio_open opens a stremio-web search in the browser", res["ok"] and opened == [BASE + "/#/search?search=sintel"])
check("a bad IMDb id is refused", not stremio_open.tool_stremio_open({"imdb_id": "x"})["ok"])
check("half an episode asks", stremio_open.tool_stremio_open({"imdb_id": "tt0944947", "season": 1}).get("needs_clarification"))
check("an unknown screen is refused", not stremio_open.tool_stremio_open({"screen": "evil"})["ok"])

# ---- playback
launched = []


class FakeProc:
    pid = 77


real_popen = player.subprocess.Popen
player.subprocess.Popen = lambda argv, **kw: (launched.append(argv) or FakeProc())
player.find_one = lambda kind: "/fake/%s" % kind if kind == "mpv" else ""
opened.clear()

res = play_media.tool_play_episode({"imdb_id": "tt0944947", "season": 1, "episode": 1})
check("tier 1: IMDb id + season + episode alone opens the stream list", res["ok"] and res["tier"] == 1 and opened == [res["link"]]
      and res["link"].endswith("#/detail/series/tt0944947/tt0944947:1:1"))
check("no IMDb id and nothing else asks which show", play_media.tool_play_episode({}).get("needs_clarification"))
check("a series without season/episode asks", play_media.tool_play_episode({"imdb_id": "tt0944947"}).get("needs_clarification"))
check("a bad IMDb id is refused", not play_media.tool_play_episode({"imdb_id": "tt1", "season": 1, "episode": 1})["ok"])

opened.clear()
launched.clear()
ST.registered.clear()
res = play_media.tool_play_episode({"infohash": H1, "imdb_id": "tt1727587", "type": "movie"})
check("tier 3: an infohash plays through mpv on the server's own link", res["ok"] and res["tier"] == 3 and res["player"] == "mpv"
      and launched and launched[0][-1] == "%s/%s/0" % (BASE, H1) and launched[0][-2] == "--" and not opened)
check("the player was started with an argument list, no shell", isinstance(launched[0], list))
check("a stream with a bad infohash is refused", not play_media.tool_play_episode({"stream": {"infoHash": "zz"}, "imdb_id": "tt1727587", "type": "movie"})["ok"])

launched.clear()
player.find_one = lambda kind: ""
res = play_media.tool_play_episode({"stream": {"infoHash": H1, "fileIdx": 0}, "imdb_id": "tt0944947", "season": 1, "episode": 1})
check("no player installed: tier 2, the stremio-web player link with the stream pre-selected",
      res["ok"] and res["tier"] == 2 and "#/player/" in res["link"] and not launched and res["link"] in opened)
res = play_media.tool_play_episode({"stream": {"infoHash": H1}, "imdb_id": "tt0944947", "season": 1, "episode": 1, "via": "player"})
check("via=player with no player says so instead of opening a browser", not res["ok"] and "mpv" in res["error"] and "tried" in res)
player.find_one = lambda kind: "/fake/%s" % kind if kind == "mpv" else ""

# a finished download
LIB.mkdir(parents=True, exist_ok=True)
store = Store()
store.set_settings({"library_root": str(LIB)})
(LIB / "Stremio").mkdir()
movie = LIB / "Stremio" / "Sintel.mp4"
movie.write_bytes(b"x" * 10)
item = store.add_item(engine="stremio", source_url="magnet:?xt=urn:btih:%s&so=0" % H1, title="Sintel", kind="video")[0]
store.claim_next({"default": 9}, 9)
store.update_item(item["id"], {"dest_file": str(movie)})
store.transition(item["id"], "done", expect="connecting") or store.transition(item["id"], "done", expect=("probing", "connecting", "downloading"))
launched.clear()
res = play_media.tool_play_local({"id": item["id"]})
check("play_local plays a finished download in the player", res["ok"] and launched and launched[0][-1] == str(movie))
launched.clear()
res = play_media.tool_play_episode({"infohash": H1, "file_idx": 0, "imdb_id": "tt1727587", "type": "movie"})
check("play_episode prefers the finished local file for the same torrent", res["ok"] and "downloaded file" in res["how"] and launched[0][-1] == str(movie))
check("play_local by title", play_media.tool_play_local({"query": "sinte"})["ok"])
check("play_local with nothing asks", play_media.tool_play_local({}).get("needs_clarification"))
check("play_local on a missing id fails", not play_media.tool_play_local({"id": "dl_nope"})["ok"])
unfinished = store.add_item(engine="stremio", source_url="magnet:?xt=urn:btih:%s" % ("22" * 20), title="x", kind="video")[0]
check("play_local refuses a download that isn't finished", "isn't finished" in play_media.tool_play_local({"id": unfinished["id"]})["error"])
exe = LIB / "Stremio" / "setup.exe"
exe.write_bytes(b"x")
store.update_item(item["id"], {"dest_file": str(exe)})
check("play_local never plays a program", not play_media.tool_play_local({"id": item["id"]})["ok"])
store.update_item(item["id"], {"dest_file": str(Path(HOME) / "outside.mp4")})
(Path(HOME) / "outside.mp4").write_bytes(b"x")
check("play_local never plays a file outside the library", not play_media.tool_play_local({"id": item["id"]})["ok"])
player.subprocess.Popen = real_popen

# ---- declarations
for mod in (stremio_stack, stremio_open, play_media):
    check("%s: schemas and TOOLS agree" % mod.__name__.split(".")[-1],
          sorted(s["name"] for s in mod.TOOL_SCHEMAS) == sorted(mod.TOOLS) and mod.TOOL_GROUP == "stremio")
    entries, _meta, problems = checklist_schema.extract_supplied(mod.TEST_CHECKLIST, getattr(mod, "TEST_CHECKLIST_GROUP", None),
                                                                 list(mod.TOOLS), "stremio")
    check("%s: every tool has a well-formed checklist entry" % mod.__name__.split(".")[-1],
          set(entries) == set(mod.TOOLS) and not problems)
check("the two destructive tools are confirm-gated", {"stremio_prepare", "server_cache_clear"} <= stremio_stack.TOOL_CONFIRM_REQUIRED)
for mod in (stremio_stack, play_media):
    ok_cmds, problems = tool_commands.validate_commands(mod.TOOL_COMMANDS, mod.__name__.split(".")[-1] + ".py", set(mod.TOOLS))
    check("%s: the palette commands validate" % mod.__name__.split(".")[-1], ok_cmds and len(ok_cmds) == len(mod.TOOL_COMMANDS) and not problems)
daemons, problems = tool_daemons.validate_daemons(stremio_stack.DAEMONS, "stremio_stack.py", str(Path(stremio_stack.__file__).parent))
check("both daemons validate", len(daemons) == 2 and not problems)
for runner in ("_stremio_server.py", "_stremio_web.py"):
    check("the runner %s exists beside the tool file" % runner, (Path(stremio_stack.__file__).parent / runner).is_file())

fake.stop(FAKE)
shutil.rmtree(HOME, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
