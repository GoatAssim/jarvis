"""Download Hub phase 4/7: the pure Stremio helpers (hub/stremio/links.py, config.py, player.py).

Magnets, infohashes (hex and base32), Stremio stream objects, the server's own URLs, the
canonical source text and the dedupe identity, picking the right file of a pack, the
stremio-web deep links and `encode_stream`, the loopback-only config rule, and what the player
launcher will and will not play.

Run: python tests/test_hub_stremio_links.py
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stremio-links-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STREMIO_SERVER_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.hub import router  # noqa: E402
from jarvis.hub.stremio import config, links, player  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


H = "08ada5a7a6183aae1e09d831df6748d566095a10"
HP = "127.0.0.1:11470"

# ---- infohashes and magnets
check("40-hex in any case normalises to lower case", links.normalize_infohash(H.upper()) == H)
b32 = base64.b32encode(bytes.fromhex(H)).decode()
check("a base32 infohash (32 letters) becomes the same hex", links.normalize_infohash(b32) == H)
check("junk is not an infohash", links.normalize_infohash("xyz") is None and links.normalize_infohash(H[:-1]) is None
      and links.normalize_infohash(None) is None)
m = links.parse_magnet("magnet:?xt=urn:btih:%s&dn=Sintel+2010&tr=udp%%3A%%2F%%2Ftracker.example%%3A1337&tr=javascript:x&so=3" % H.upper())
check("a magnet gives hash, name, file index and only sane trackers",
      m["infohash"] == H and m["name"] == "Sintel 2010" and m["file_idx"] == 3 and m["trackers"] == ["udp://tracker.example:1337"])
check("so=0,2-4 (several files) leaves the pick to us", links.parse_magnet("magnet:?xt=urn:btih:%s&so=0,2-4" % H)["file_idx"] is None)
check("a magnet with no btih is refused", links.parse_magnet("magnet:?dn=x") is None and links.parse_magnet("http://x") is None)

# ---- inputs
check("a bare infohash is a torrent", links.parse_input(H)["kind"] == "infohash")
check("a bare 32-letter word is NOT taken for base32", links.parse_input(b32) is None)
s = links.parse_input('{"infoHash":"%s","fileIdx":5,"sources":["tracker:udp://a.b:1","dht:%s"],"behaviorHints":{"filename":"x.mkv"}}' % (H, H))
check("a Stremio stream object is understood (file index, trackers, name)",
      s["kind"] == "stream" and s["file_idx"] == 5 and s["trackers"] == ["udp://a.b:1"] and s["name"] == "x.mkv")
check("a stream object without infoHash (a direct url) is not ours", links.parse_input('{"url":"https://x/y.mp4"}') is None)
check("a Stremio server URL with an index", links.parse_input("http://127.0.0.1:11470/%s/7?external=1" % H, HP)["file_idx"] == 7)
check("a server URL with -1 means 'pick' (no index)", links.parse_input("http://127.0.0.1:11470/%s/-1" % H, HP)["file_idx"] is None)
check("another port or host is not the Stremio server", links.parse_input("http://127.0.0.1:9999/%s/1" % H, HP) is None
      and links.parse_input("http://evil.example:11470/%s/1" % H, HP) is None)
check("a .torrent link is recognised", links.parse_input("https://x.org/a/b.torrent", HP)["kind"] == "torrent_url")
check("an ordinary file link is not", links.parse_input("https://x.org/a.zip", HP) is None)

# ---- canonical text and identity
canon = links.canonical_source(links.parse_input("magnet:?xt=urn:btih:%s&dn=A&tr=udp://t:1&so=2" % H.upper()))
check("the canonical source is a magnet with hash, file index, name and trackers",
      canon == "magnet:?xt=urn:btih:%s&so=2&dn=A&tr=udp%%3A%%2F%%2Ft%%3A1" % H)
check("identity is hash + index, never trackers or name", links.identity(canon) == "btih:%s:2" % H
      and links.identity("magnet:?xt=urn:btih:%s&tr=udp://z:2" % H) == "btih:%s" % H)
check("router.canonical_url makes two pastes of one torrent one download",
      router.canonical_url(canon) == router.canonical_url("magnet:?xt=urn:btih:%s&so=2" % H.upper()))
check("a different file of the same torrent is a different download",
      router.canonical_url("magnet:?xt=urn:btih:%s&so=1" % H) != router.canonical_url("magnet:?xt=urn:btih:%s&so=2" % H))
check("ordinary URLs still canonicalise as before", router.canonical_url("HTTP://Example.com:80/a#f") == "http://example.com/a")

# ---- the server's URLs
check("download url is Stremio's own 'Download video' form", links.download_url("http://127.0.0.1:11470/", H, 5) ==
      "http://127.0.0.1:11470/%s/5?external=1&download=1" % H)
check("no index means -1 (largest)", links.stream_url("http://127.0.0.1:11470", H, None).endswith("/-1"))
body = links.create_body(H, ["udp://a:1"])
check("create body matches what stremio-core sends", body == {"peerSearch": {"min": 40, "max": 200, "sources": ["dht:" + H, "tracker:udp://a:1"]}})

# ---- picking a file
pack = [{"path": "Show/Show.S01E01.1080p.mkv", "length": 900}, {"path": "Show/Show.S01E02.1080p.mkv", "length": 950},
        {"path": "Show/Sample/sample.S01E02.mkv", "length": 20}, {"path": "Show/readme.txt", "length": 5000}]
check("the named index wins", links.pick_file(pack, 0) == (0, "the file the source named"))
check("an index past the end is refused with a reason", links.pick_file(pack, 9)[0] is None)
check("S01E02 picks that episode, not the sample", links.pick_file(pack, None, 1, 2)[0] == 1)
check("an episode that isn't there says so", links.pick_file(pack, None, 3, 3)[0] is None)
check("no hint: the largest VIDEO file, not the bigger .txt", links.pick_file(pack)[0] == 1)
check("an empty file list says it has none yet", links.pick_file([])[0] is None)
check("1x02 and 'Season 1 Episode 2' also read as episodes", links.episode_of("show.1x02.avi") == (1, 2)
      and links.episode_of("Show Season 1 Episode 2.mkv") == (1, 2))
check("a file name is only the last path part", links.file_name({"path": "a/b\\c.mkv"}) == "c.mkv")

# ---- stremio-web links
check("encode_stream round-trips", links.decode_stream(links.encode_stream({"infoHash": H, "fileIdx": 2})) == {"infoHash": H, "fileIdx": 2})
check("encode_stream is percent-encoded base64 of a stored zlib block",
      "%" in links.encode_stream({"a": "é" * 50}) or "+" not in links.encode_stream({"a": 1}))
web = "http://127.0.0.1:8080"
check("episode deep link", links.deep_link_detail(web, "tt0944947", "series", 1, 2) == web + "/#/detail/series/tt0944947/tt0944947:1:2")
check("show deep link, movie deep link", links.deep_link_detail(web, "tt0944947") == web + "/#/detail/series/tt0944947"
      and links.deep_link_detail(web, "tt1727587", "movie") == web + "/#/detail/movie/tt1727587/tt1727587")
for bad in ("tt12", "abc", "tt0944947; rm", "http://evil/x"):
    try:
        links.deep_link_detail(web, bad)
        check("a bad IMDb id is refused: %r" % bad, False)
    except ValueError:
        check("a bad IMDb id is refused: %r" % bad, True)
check("the search screen escapes its text", links.screen_link(web, "search", "a b&c") == web + "/#/search?search=a%20b%26c")
try:
    links.screen_link(web, "evil")
    check("an unknown screen is refused", False)
except ValueError:
    check("an unknown screen is refused", True)
pl = links.player_link(web, {"infoHash": H}, "tt0944947", 1, 2, "https://v3-cinemeta.strem.io/manifest.json")
check("the player link has the plan's segments", pl.startswith(web + "/#/player/") and pl.count("/") >= 10 and "tt0944947%3A1%3A2" in pl)

# ---- config: loopback only
check("defaults: loopback server and web", config.server_url() == "http://127.0.0.1:11470" and config.web_url() == "http://127.0.0.1:8080")
check("a LAN or public host is not accepted as the server", config._loopback_url("http://192.168.1.5:11470") is None
      and config._loopback_url("http://evil.example:11470") is None and config._loopback_url("https://127.0.0.1:11470") is None
      and config._loopback_url("http://user:pw@127.0.0.1:11470") is None)
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://10.0.0.5:1"
check("the env override obeys the same rule", config.server_url() == "http://127.0.0.1:11470")
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:12345"
check("a loopback override is used and gives the allow-list entry", config.server_url() == "http://127.0.0.1:12345"
      and config.server_hostport() == "127.0.0.1:12345")
os.environ.pop("JARVIS_STREMIO_SERVER_URL")
path = config.config_path()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text('{"server": {"url": "http://192.168.0.9:11470"}, "account": {"auth_key": "SECRET123"}, "web": {"dir": ""}}')
check("a hand-edited non-loopback server url is ignored and reported", config.server_url() == "http://127.0.0.1:11470"
      and any("server.url" in p for p in config.problems()))
check("public_view never shows the account key", "SECRET123" not in str(config.public_view()) and config.public_view()["account"]["auth_key"] == "(set)")
path.write_text("{not json")
check("an unreadable config is just the defaults", config.server_url() == "http://127.0.0.1:11470")

# ---- the player
lib = Path(HOME) / "lib"
(lib / "Video").mkdir(parents=True)
ok_file = lib / "Video" / "a.mkv"
ok_file.write_bytes(b"x")
exe = lib / "Video" / "run.exe"
exe.write_bytes(b"x")
outside = Path(HOME) / "elsewhere.mkv"
outside.write_bytes(b"x")
check("a video under the library may be played", player.check_target(str(ok_file), roots=[lib])[0])
check("an .exe is refused", not player.check_target(str(exe), roots=[lib])[0])
check("a video outside the library is refused", not player.check_target(str(outside), roots=[lib])[0])
check("a missing file is refused", not player.check_target(str(lib / "nope.mkv"), roots=[lib])[0])
check("the server's own stream link may be played", player.check_target("http://127.0.0.1:11470/%s/3" % H)[0])
for bad in ("http://127.0.0.1:11470/%s/3?x=1" % H, "http://evil.example/%s/3" % H, "http://127.0.0.1:11470/zzz/3",
            "http://127.0.0.1:11470/%s/abc" % H, "http://127.0.0.1:11470/%s" % H, "ftp://127.0.0.1:11470/%s/3" % H):
    check("a link that isn't the server's /<hash>/<n> is refused: %s" % bad[-30:], not player.check_target(bad)[0])
argv = player.build_command("mpv", "mpv", "http://127.0.0.1:11470/%s/3" % H, True, "/tmp/sock")
check("mpv gets an IPC endpoint, fullscreen, and `--` before the target", argv[-2:] == ["--", "http://127.0.0.1:11470/%s/3" % H]
      and "--input-ipc-server=/tmp/sock" in argv and "--fs" in argv)
check("vlc gets the target as its last argument", player.build_command("vlc", "vlc", "x.mkv")[-1] == "x.mkv")
popped = []


class FakeProc:
    pid = 4242


res = player.launch("mpv", "mpv", str(ok_file), popen=lambda argv, **kw: (popped.append((argv, kw)) or FakeProc()))
check("launch starts the player with an argument list (no shell) and records it",
      res["ok"] and res["pid"] == 4242 and isinstance(popped[0][0], list) and not popped[0][1].get("shell")
      and player.state_path().exists())
check("launch reports a player that won't start", not player.launch("mpv", "mpv", "x", popen=lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))["ok"])
opened = []
def _opener(url):
    opened.append(url)
    return True


check("only a link on stremio-web is opened", player.open_in_browser(web + "/#/library", opener=_opener)[0] and opened
      and not player.open_in_browser("https://evil.example/", opener=_opener)[0] and len(opened) == 1)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
