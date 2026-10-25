"""Download Hub round 8, brief 8A: `play_episode` does the whole flow in one call (N25), keeps the file index (N26)
and fails honestly (N27).

Covers (every one against FAKES: the fake addon pair, the fake Stremio server, a fake player process and, where a test
needs a server that never answers, a scripted stand-in for `StremioServer`; nothing here ran against a real addon, a real
Stremio server, a real mpv / VLC or Windows; no network):

  * one call, a title + season + episode + quality + audio, starts the (fake) player: no media_find, no stream_search,
    no episode list, no page; the answer names the release, why it was chosen, and what was tried;
  * nothing exact: a question with one option per kind, nothing started, nothing opened; with a (fake) pick store the
    options carry a `pick` number, and playing that pick works;
  * the first release gives no file list / never answers inside its budget and the next one plays; a file index given
    means no file-list wait at all; an index the torrent does not have fails that release only;
  * every release fails: ok False, `tried` (one line each, three at most), a `server` line, no page, no player, and
    the answer never says "playing"; a setup failure (no player, server down, the player will not start) stops at once;
  * `pick` with and without a remembered search, with the pick-store module missing, and with a bad number;
  * the old paths: `stream` / `infohash` / a stream_search row passed unchanged still play; their page fallback is
    kept, now with the real reason and a server line; `via=stremio` and `open=false` never search;
  * both branches of each helper that calls another agent's new keyword (streams_for intent, rank intent,
    normalize_want language), and of the title-cache and pick-store imports (module present, module missing);
  * the poll helper (`server.wait_for_files`) on a fake clock: short polls, a budget it never passes, each call's timeout;
  * a chat guest is refused by the handler; the declarations (schema, checklist, no new tool).

Run: python tests/test_hub_play_flow.py
"""
import json
import os
import sys
import tempfile
import time
import types
import webbrowser
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-play-flow-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_addon as fake_addon  # noqa: E402
import _hub_fake_stremio as fake  # noqa: E402
from jarvis import checklist_schema  # noqa: E402
from jarvis.actions import play_media, stremio_stack  # noqa: E402
from jarvis.actions import stremio_discovery as disc  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import addons as A  # noqa: E402
from jarvis.hub.stremio import config, discovery, play_flow, player, ranking  # noqa: E402
from jarvis.hub.stremio import server as sserver  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


# ------------------------------------------------------------------ the fakes
FAKE, BASE, ST = fake.start()
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
ABASE, AST, ASERVER = fake_addon.start()
A.DEFAULT_META_MANIFEST = "http://127.0.0.1:9/manifest.json"      # Cinemeta's stand-in: nothing listens on port 9
Store().set_settings({"allow_private_hosts": ["127.0.0.1"]})        # lets the loopback fakes through the guard
cfg_path = config.config_path()
cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(json.dumps({"web": {"url": BASE}, "addons": {"urls": [ABASE + "/cat/manifest.json", ABASE + "/str/manifest.json"]}}))

opened, launched = [], []
webbrowser.open = lambda url: (opened.append(url) or True)


class FakeProc:
    pid = 77


player.subprocess.Popen = lambda argv, **kw: (launched.append(argv) or FakeProc())
player.find_one = lambda kind: "/fake/%s" % kind if kind == "mpv" else ""

SH = fake_addon.SHOW
GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})

# a short budget keeps the tests quick; the real numbers are asserted separately
REAL_BUDGETS = (play_flow.BUDGET_WITH_INDEX_S, play_flow.BUDGET_NO_INDEX_S, sserver.POLL_EVERY_S)
play_flow.BUDGET_WITH_INDEX_S = play_flow.BUDGET_NO_INDEX_S = 0.6
sserver.POLL_EVERY_S = 0.1

# episodes the stock fake addon does not have: the test chooses what the addon answers
_orig_streams_for = fake_addon.streams_for
CUSTOM = {}


def _streams_for(video_id, addon):
    if video_id in CUSTOM:
        return CUSTOM[video_id].get(addon, [])
    return _orig_streams_for(video_id, addon)


fake_addon.streams_for = _streams_for


def release(ep, desc, seeders, idx=None, size=1.0, label="1080p"):
    stream = fake_addon._stream(label, 2, ep, desc, size_gb=size, seeders=seeders, idx=idx)
    return stream, stream["infoHash"]


def vid(ep):
    return "%s:2:%d" % (SH, ep)


def serve(digest, ep=5, files=None):
    ST.torrents[digest] = {"files": files or [("Show/Spirit.Hunters.S02E%02d.mkv" % ep, 5000)]}


def dead(digest):
    ST.torrents[digest] = {"dead": True, "files": []}


def reset():
    ST.registered.clear()
    ST.log.clear()
    AST.log.clear()
    launched.clear()
    opened.clear()


def stream_requests():
    return [x for x in AST.log if "/stream/" in x]


def call(**kw):
    t0 = time.monotonic()
    res = play_media.tool_play_episode(dict(kw))
    return res, time.monotonic() - t0


def base_args(ep, **more):
    out = {"imdb_id": SH, "season": 2, "episode": ep, "quality": "1080p", "audio": "dub"}
    out.update(more)
    return out


# four exact releases for episode 5, best first (the stock fake has at most two)
EP5 = [release(5, "1080p WEB-DL Dub %s" % n, s) for n, s in (("A", 90), ("B", 80), ("C", 70), ("D", 60))]
CUSTOM[vid(5)] = {"/cat": [r[0] for r in EP5]}
HA, HB, HC, HD = (r[1] for r in EP5)
# episode 6: the best exact release names its file (a pack), a second one names another
EP6 = [release(6, "1080p BluRay Dub pack", 50, idx=2), release(6, "1080p WEB-DL Dub x265", 40, idx=1)]
CUSTOM[vid(6)] = {"/cat": [r[0] for r in EP6]}
# episode 7: nothing exact: a 1080p sub and a 720p dub
EP7 = [release(7, "1080p WEB-DL Eng Sub", 100), release(7, "720p Dubbed", 40, label="720p", size=0.6)]
CUSTOM[vid(7)] = {"/cat": [r[0] for r in EP7]}
# episode 8: the addons know of nothing
CUSTOM[vid(8)] = {"/cat": []}
# episode 9: the best release names a file the torrent does not have, the second names one it does
EP9 = [release(9, "1080p WEB-DL Dub bad index", 90, idx=5), release(9, "1080p WEB-DL Dub good index", 80, idx=1)]
CUSTOM[vid(9)] = {"/cat": [r[0] for r in EP9]}
# episode 10: a finished download of the same torrent exists
EP10 = [release(10, "1080p WEB-DL Dub Z", 90)]
CUSTOM[vid(10)] = {"/cat": [r[0] for r in EP10]}


class ScriptServer:
    """Stands in for sserver.StremioServer where a test needs a server that does not answer, or a scripted file list.
    script[hash] = 'silent' (create is held back, stats knows nothing) | 'nofiles' | a files list."""
    script = {}
    up = (True, "4.22.3")
    calls = []
    timeouts = []

    def __init__(self, base_url=None, session=None, timeout=5.0):
        self.base = BASE

    def is_up(self, timeout=None):
        return ScriptServer.up

    def _note(self, timeout):
        for part in (timeout if isinstance(timeout, tuple) else (timeout,)):
            if isinstance(part, (int, float)):
                ScriptServer.timeouts.append(float(part))

    def create(self, infohash, trackers=(), timeout=(5, 15)):
        ScriptServer.calls.append(("create", infohash))
        self._note(timeout)
        what = ScriptServer.script.get(infohash, "silent")
        if what == "silent":
            raise sserver.ServerBusy("the Stremio server didn't answer /create in time")
        return {"infoHash": infohash, "files": None if what == "nofiles" else what}

    def stats(self, infohash, timeout=None):
        ScriptServer.calls.append(("stats", infohash))
        self._note(timeout)
        what = ScriptServer.script.get(infohash, "silent")
        if what == "silent":
            return None
        return {"infoHash": infohash, "files": None if what == "nofiles" else what}


def files_of(*sizes, ep=5):
    return [{"path": "Show/Spirit.Hunters.S02E%02d.%d.mkv" % (ep, i), "name": "x%d.mkv" % i, "length": n} for i, n in enumerate(sizes)]


REAL_SERVER = sserver.StremioServer
REAL_START_SERVER = stremio_stack._start_server


class scripted:
    def __init__(self, script, up=(True, "4.22.3")):
        self.script, self.up = script, up

    def __enter__(self):
        ScriptServer.script, ScriptServer.up = dict(self.script), self.up
        ScriptServer.calls, ScriptServer.timeouts = [], []
        sserver.StremioServer = ScriptServer
        return ScriptServer

    def __exit__(self, *exc):
        sserver.StremioServer = REAL_SERVER
        stremio_stack._start_server = REAL_START_SERVER


PICK = "jarvis.hub.stremio.pick_store"
TITLES = "jarvis.hub.stremio.title_cache"
sys.modules[PICK] = None          # the module does not exist in this build: an import fails (set per test where one is needed)
sys.modules[TITLES] = None
MEM = {}
RECALLS = []


def fake_pick_module():
    mod = types.ModuleType(PICK)
    mod.remember = lambda video_id, candidates: MEM.__setitem__(video_id, [dict(c) for c in candidates])

    def recall(video_id, rank):
        RECALLS.append((video_id, rank))
        rows = MEM.get(video_id) or []
        return rows[rank - 1] if 1 <= rank <= len(rows) else None
    mod.recall = recall
    return mod


FAILED_ANSWERS = []


def said_playing(res):
    return "playing" in json.dumps(res).lower()


# ------------------------------------------------------------------ 1. one call plays
reset()
serve(HA)
res, took = call(title="Spirit Hunters", season=2, episode=5, quality="1080p", audio="dub")
check("one play_episode call (title, season, episode, quality, audio) starts the fake player",
      res.get("ok") is True and res.get("played") is True and res["tier"] == 3 and res["player"] == "mpv" and len(launched) == 1)
check("...on the server's own link for the best release, the file the episode name points at",
      launched and launched[0][-1] == "%s/%s/0" % (BASE, HA) and launched[0][-2] == "--")
check("...and nothing was opened in a browser", opened == [])
check("...the streams of that ONE episode were asked for, and no episode list (meta) was read",
      any("/stream/series/%s" % vid(5) in x for x in AST.log) and not any("/meta/" in x for x in AST.log))
check("...the answer names the release: label, the addon, the seeders the addon listed, and why it was chosen",
      res["release"]["label"] == "1080p dub" and res["release"]["addon"] == "Fake Catalog" and res["release"]["seeders_listed"] == 90
      and "exact" in res["why"] and "listed 90 seeders" in res["why"])
check("...it says what the title was taken to be, the video id, what was searched, and what was tried",
      "Spirit Hunters" in res["matched"] and res["video_id"] == vid(5) and res["searched"]["exact"] == 4 and len(res["tried"]) == 1
      and res["tried"][0].startswith("#1 "))
check("...four exact releases existed and exactly one was started", ST.registered == [HA])

# ------------------------------------------------------------------ 2. nothing exact asks, starts nothing
reset()
res, _ = call(**base_args(7))
check("no exact release: a question, with nothing started and nothing opened",
      res.get("needs_clarification") is True and launched == [] and opened == [] and ST.registered == [] and "ok" not in res)
check("...one option per kind, each with the quality and audio to ask again with",
      {o["tier"] for o in res["options"]} == {"other_quality", "other_audio"} and all("ask_with" in o for o in res["options"]))
check("...without a pick store the options carry no pick number", all("pick" not in o for o in res["options"]))
check("...the message says nothing was started and names what was asked (1080p dub)",
      "nothing was started" in res["message"] and "1080p dub" in res["message"])
alt = next(o for o in res["options"] if o["tier"] == "other_quality")
serve(EP7[1][1], ep=7)
reset()
res, _ = call(**base_args(7, quality=alt["ask_with"]["quality"], audio=alt["ask_with"]["audio"]))
check("asking again with the option's ask_with plays that release", res.get("ok") is True and launched and launched[0][-1].startswith("%s/%s/" % (BASE, EP7[1][1])))
FAILED_ANSWERS.append(call(**base_args(7))[0])

# ...and with a pick store the options carry pick numbers that play
sys.modules[PICK] = fake_pick_module()
MEM.clear()
RECALLS.clear()
reset()
res, _ = call(**base_args(7))
picks = {o["tier"]: o.get("pick") for o in res["options"]}
check("with a pick store the search is remembered under the video id and the options carry pick numbers",
      vid(7) in MEM and [c["tier"] for c in MEM[vid(7)]] == ["other_quality", "other_audio"] and picks == {"other_quality": 1, "other_audio": 2})
check("...a remembered candidate has what the contract names (stream, infohash, file_idx, name, label, tier, size, seeders, addon)",
      all(k in MEM[vid(7)][0] for k in ("stream", "infohash", "file_idx", "name", "label", "tier", "size", "seeders", "addon")))
reset()
res, _ = call(imdb_id=SH, season=2, episode=7, pick=1)
check("playing a pick plays that remembered release and asks no addon for streams",
      res.get("ok") is True and res["played"] is True and launched and EP7[1][1] in launched[0][-1] and stream_requests() == [])
check("...the pick store was asked for (video id, rank)", RECALLS[-1] == (vid(7), 1))
check("...and the answer says it was the release that was picked", res["why"].startswith("the release you picked (number 1"))

# ------------------------------------------------------------------ 3. a release that gives no files, or never answers, costs its budget and the next one plays
sys.modules[PICK] = None
reset()
dead(HA)
serve(HB)
res, took = call(**base_args(5))
check("the first release answers with no file list: the second plays", res.get("ok") is True and launched and launched[0][-1] == "%s/%s/0" % (BASE, HB))
check("...the answer lists both, the first with its real reason (no files in the budget), not 'no seeders'",
      len(res["tried"]) == 2 and "listed no files" in res["tried"][0] and "may have no seeders" not in json.dumps(res) and res["tried"][1].startswith("#2 "))
check("...the seeders appear only as what the addon listed", "Fake Catalog listed 90 seeders" in res["tried"][0])
check("...it took about one budget, not a 20 s block (budget 0.6 s here)", took < 8.0)

reset()
with scripted({HA: "silent", HB: files_of(5000)}) as srv:
    res, took = call(**base_args(5))
    waited_calls = list(srv.calls)
    longest = max(srv.timeouts or [0.0])
check("a server that never answers about the first release: the second plays",
      res.get("ok") is True and launched and launched[0][-1] == "%s/%s/0" % (BASE, HB))
check("...the reason is 'no answer from the Stremio server', the budget stayed small and no single call waited long",
      "no answer from the Stremio server" in res["tried"][0] and took < 6.0 and longest <= sserver.CREATE_WAIT_S)
check("...it asked in short polls: several stats calls, not one long block", sum(1 for c in waited_calls if c[0] == "stats" and c[1] == HA) >= 3)

# ------------------------------------------------------------------ 4. the file index goes all the way; a named file is not waited for
reset()
dead(EP6[0][1])
t0 = time.monotonic()
res, took = call(**base_args(6))
check("a stream that names its file starts at once even though the server lists no files yet",
      res.get("ok") is True and launched and launched[0][-1] == "%s/%s/2" % (BASE, EP6[0][1]) and took < 3.0)
check("...no file-list poll was made (no stats.json request)", not any("stats.json" in path for _m, path, _r in ST.log))
check("...the file index is in the answer", res["release"]["file_idx"] == 2)

reset()
serve(EP9[0][1], ep=9, files=[("a/Spirit.Hunters.S02E09.mkv", 5000), ("a/other.mkv", 100)])
serve(EP9[1][1], ep=9, files=[("a/Spirit.Hunters.S02E09.mkv", 5000), ("a/other.mkv", 100)])
res, _ = call(**base_args(9))
check("an index the torrent does not have fails that release only, with the real sentence; the next one plays its own index",
      res.get("ok") is True and "names file 5 but the torrent has 2 file(s)" in res["tried"][0]
      and launched[0][-1] == "%s/%s/1" % (BASE, EP9[1][1]))

# ------------------------------------------------------------------ 5. every release fails: honest
reset()
for d in (HA, HB, HC, HD):
    dead(d)
res, took = call(**base_args(5))
FAILED_ANSWERS.append(res)
check("every release fails: ok False, played False", res.get("ok") is False and res.get("played") is False)
check("...three releases were tried, one line each, the fourth never", len(res["tried"]) == 3 and ST.registered == [HA, HB, HC])
check("...the answer carries the server line (the fake server's version)", res.get("server") == "up v4.21.2")
check("...no page opened, no player started, no link in the answer", opened == [] and launched == [] and "link" not in res)
check("...it says nothing was opened and how to open the stream list if the owner wants it",
      "Nothing was opened" in res["error"] and 'via="stremio"' in res["error"] and "None of the 3 releases" in res["error"])
check("...the whole call cost a few budgets, never three 20 s blocks", took < 9.0)

# a finished download of the same torrent is played from the file
LIB = Path(HOME) / "lib"
(LIB / "Stremio").mkdir(parents=True, exist_ok=True)
store = Store()
store.set_settings({"library_root": str(LIB)})
movie = LIB / "Stremio" / "Spirit.Hunters.S02E10.mkv"
movie.write_bytes(b"x" * 10)
item = store.add_item(engine="stremio", source_url="magnet:?xt=urn:btih:%s&so=0" % EP10[0][1], title="Spirit Hunters S02E10", kind="video")[0]
store.claim_next({"default": 9}, 9)
store.update_item(item["id"], {"dest_file": str(movie)})
store.transition(item["id"], "done", expect="connecting") or store.transition(item["id"], "done", expect=("probing", "connecting", "downloading"))
reset()
res, _ = call(**base_args(10))
check("a finished download of the chosen release is played from its file, the server is not asked",
      res.get("ok") is True and "downloaded file" in res["how"] and launched and launched[0][-1] == str(movie) and ST.registered == [])

# ------------------------------------------------------------------ 6. setup failures stop at once and say so
reset()
for d in (HA, HB, HC, HD):
    dead(d)
saved_find = player.find_one
player.find_one = lambda kind: ""
res, _ = call(**base_args(5))
FAILED_ANSWERS.append(res)
check("no mpv or VLC: ok False before any addon is asked, with the server line and the page offer",
      res.get("ok") is False and "mpv" in res["error"] and res.get("server") == "up v4.21.2" and stream_requests() == []
      and "Nothing was opened" in res["error"] and opened == [] and launched == [])
player.find_one = saved_find

reset()
stremio_stack._start_server = lambda cfg, srv: {"state": "error", "error": "the Stremio server isn't running (test)"}
with scripted({HA: files_of(5000)}, up=(False, "refused")):
    res, _ = call(**base_args(5))
FAILED_ANSWERS.append(res)
check("the server is down and cannot be started: it stops after the first release, says why, and the server line is 'down'",
      res.get("ok") is False and res["server"] == "down" and len(res["tried"]) == 1 and "isn't running" in res["error"]
      and res["error"].startswith("Could not start playback") and launched == [] and opened == [])

real_popen = player.subprocess.Popen


def _boom(argv, **kw):
    raise OSError("no such program")


serve(HA)
reset()
player.subprocess.Popen = _boom
try:
    res, _ = call(**base_args(5))
finally:
    player.subprocess.Popen = real_popen
FAILED_ANSWERS.append(res)
check("a player that will not start stops after the first release (it is not about the release) and says so",
      res.get("ok") is False and len(res["tried"]) == 1 and "couldn't start mpv" in res["error"] and opened == [])

reset()
res, _ = call(**base_args(8))
FAILED_ANSWERS.append(res)
check("no release at all: ok False, one sentence, no player, no page, and a hint that the episode may not exist",
      res.get("ok") is False and "no usable torrent release" in res["error"] and "media_episodes" in res["error"] and launched == [] and opened == [])
reset()
res, _ = call(imdb_id=SH, season=2, episode=5, quality="1080p", audio="dub", min_seeders=500)
FAILED_ANSWERS.append(res)
check("everything filtered out by min_seeders: ok False, what was rejected is in the answer", res.get("ok") is False and res.get("rejected"))

reset()
res, _ = call(imdb_id=SH, season=2, episode=5, quality="potato")
check("a bad quality is refused before any addon is asked", res.get("ok") is False and "quality must be one of" in res["error"] and stream_requests() == [] and launched == [])

check("none of the failure answers says 'playing'", not any(said_playing(r) for r in FAILED_ANSWERS))
check("...and the code no longer blames seeders for every file failure", "may have no seeders" not in Path(play_media.__file__).read_text(encoding="utf-8"))

# ------------------------------------------------------------------ 7. pick: with a search, without, no module, a bad number
reset()
serve(HA)
serve(HB)
sys.modules[PICK] = fake_pick_module()
MEM.clear()
RECALLS.clear()
res, _ = call(**base_args(5))
check("a play that searched remembered its ranked list for pick", [c["infohash"] for c in MEM[vid(5)]][:2] == [HA, HB])
reset()
res, _ = call(imdb_id=SH, season=2, episode=5, pick=2)
check("pick=2 plays the second ranked release of the remembered search", res.get("ok") is True and launched[0][-1] == "%s/%s/0" % (BASE, HB))
reset()
res, _ = call(imdb_id=SH, season=2, episode=6, pick=1)
check("a pick for an episode with no remembered search: one sentence, nothing started",
      res.get("ok") is False and res["error"] == "no search is remembered for that episode: call stream_search first" and launched == [])
MEM[vid(6)] = [dict(c) for c in [{"stream": EP6[0][0], "infohash": EP6[0][1], "file_idx": 2, "name": "n", "label": "1080p dub", "tier": "exact",
                                 "size": 10 ** 9, "seeders": 5, "addon": "Fake Catalog"}]]
dead(EP6[0][1])
reset()
res, _ = call(imdb_id=SH, season=2, episode=6, pick=1)
check("pick=1 of a remembered search plays with the file index kept, without waiting for a file list",
      res.get("ok") is True and launched and launched[0][-1] == "%s/%s/2" % (BASE, EP6[0][1]) and not any("stats.json" in p for _m, p, _r in ST.log))
reset()
res, _ = call(imdb_id=SH, season=2, episode=5, pick=7)
check("a pick number past the list: the same one sentence, nothing started", res.get("ok") is False and "no search is remembered" in res["error"] and launched == [])
sys.modules[PICK] = None
reset()
res, _ = call(imdb_id=SH, season=2, episode=5, pick=1)
check("with the pick-store module missing: the same one sentence, nothing started",
      res.get("ok") is False and res["error"] == "no search is remembered for that episode: call stream_search first" and launched == [] and stream_requests() == [])
for bad in (0, -1, "x", True, 1.5):
    reset()
    res, _ = call(imdb_id=SH, season=2, episode=5, pick=bad)
    check("pick=%r is refused before any lookup" % (bad,), res.get("ok") is False and "pick must be a whole number from 1" in res["error"] and AST.log == [])
reset()
res, _ = call(title="Spirit Hunters", type="series", pick=1)
check("pick with a series and no season/episode still asks first, with no lookup", res.get("needs_clarification") and AST.log == [])

# ------------------------------------------------------------------ 8. the old paths
reset()
serve(HA)
res, _ = call(stream={"infoHash": HA, "fileIdx": 0}, imdb_id=SH, season=2, episode=5)
check("the old path: a stream object still plays, and says it was played", res.get("ok") is True and res["tier"] == 3 and res["played"] is True
      and launched and launched[0][-1] == "%s/%s/0" % (BASE, HA) and stream_requests() == [])
reset()
res, _ = call(infohash=HA, imdb_id=SH, season=2, episode=5)
check("the old path: an infohash (no stream object) still plays", res.get("ok") is True and res["tier"] == 3 and launched)
real_row = disc.tool_stream_search({"id": SH, "type": "series", "season": 2, "episode": 2, "quality": "1080", "audio": "dub", "limit": 5})
row = real_row["streams"][0]
ROW_HASH = fake_addon.infohash(2, 2, "1080p", "1080p BluRay Dubbed")
serve(ROW_HASH, ep=2)
reset()
res, _ = call(stream=row, imdb_id=SH, season=2, episode=2)
check("a stream_search row, passed unchanged, plays (the file index it carries is not lost)",
      res.get("ok") is True and res["tier"] == 3 and launched and launched[0][-1] == "%s/%s/0" % (BASE, ROW_HASH))

dead_hash = "cd" * 20
dead(dead_hash)
reset()
res, _ = call(stream={"infoHash": dead_hash}, imdb_id=SH, season=2, episode=5)
check("the old path keeps its page fallback, now with the real reason, a server line and played: false",
      res.get("ok") is True and res["tier"] == 2 and res["played"] is False and "listed no files" in res["tried"][0]
      and "may have no seeders" not in json.dumps(res) and res.get("server") == "up v4.21.2" and opened)
reset()
res, _ = call(stream={"infoHash": dead_hash}, imdb_id=SH, season=2, episode=5, via="player")
check("via=player on the old path fails with the reason and the server line, and opens nothing",
      res.get("ok") is False and res["played"] is False and "listed no files" in res["error"] and res.get("server") == "up v4.21.2" and opened == [])

# a link / page request never searches
reset()
res, _ = call(imdb_id=SH, season=2, episode=5, quality="1080p", audio="dub", open=False)
check("open=false builds the stream-list link: no search, no player, no page, and the answer says nothing played",
      res.get("ok") is True and res["tier"] == 1 and res["played"] is False and res["opened"] is False and stream_requests() == [] and launched == [] and opened == [])
reset()
res, _ = call(imdb_id=SH, season=2, episode=5, via="stremio")
check("via=stremio opens the stream list and searches nothing", res.get("ok") is True and res["tier"] == 1 and opened == [res["link"]] and stream_requests() == [] and launched == [])

# a film by title keeps its order: looked up first, then the flow
AST.movies = True
reset()
res, _ = call(title="Paper Lanterns")
check("a film by title is looked up first and then goes through the flow (this fake offers no film streams: one sentence, nothing opened)",
      res.get("ok") is False and "no installed addon offers streams" in res["error"] and res["video_id"] == "tt7000100" and "Paper Lanterns" in res["matched"]
      and opened == [] and launched == [] and stream_requests() == [])
AST.movies = False
reset()
res, _ = call(title="Spirit Hunters", type="series")
check("a series with no season or episode still asks BEFORE any lookup", res.get("needs_clarification") and AST.log == [] and launched == [])

# ------------------------------------------------------------------ 9. helpers with another agent's new keyword: both branches
calls = []
orig = discovery.streams_for
discovery.streams_for = lambda net, found, type_, video_id, *, budget_s=1, intent="bulk": (calls.append(("new", intent)) or {"streams": []})
play_flow._streams_for(None, [], "series", vid(5))
discovery.streams_for = lambda net, found, type_, video_id, *, budget_s=1: (calls.append(("old",)) or {"streams": []})
play_flow._streams_for(None, [], "series", vid(5))
discovery.streams_for = orig
check("streams_for: a callee with `intent` is called with intent='play'; one without is called as today", calls == [("new", "play"), ("old",)])

calls = []
orig = ranking.rank
ranking.rank = lambda releases, want, *, batch_binge=None, torrents_only=True, intent="bulk": (calls.append(("new", intent)) or ([], {}))
play_flow._rank([], {})
ranking.rank = lambda releases, want, *, batch_binge=None, torrents_only=True: (calls.append(("old",)) or ([], {}))
play_flow._rank([], {})
ranking.rank = orig
check("rank: a callee with `intent` is called with intent='play'; one without is called as today", calls == [("new", "play"), ("old",)])

calls = []
orig = ranking.normalize_want
ranking.normalize_want = lambda q=None, a=None, g=None, s=None, language=None: (calls.append(("new", language)) or ({"resolution": None, "audio": "any", "max_gb": None, "min_seeders": None, "language": language}, None))
want, problem, notes = play_flow._want({"quality": "1080p", "language": "english"})
ranking.normalize_want = lambda q=None, a=None, g=None, s=None: (calls.append(("old",)) or ({"resolution": None, "audio": "any", "max_gb": None, "min_seeders": None}, None))
want2, problem2, notes2 = play_flow._want({"quality": "1080p", "language": "english"})
want3, problem3, notes3 = play_flow._want({"quality": "1080p"})
ranking.normalize_want = orig
check("normalize_want: a callee with `language` gets it and no note is made; one without is called as today and the answer says language was not used",
      calls == [("new", "english"), ("old",), ("old",)] and notes == [] and len(notes2) == 1 and "was not used" in notes2[0] and notes3 == [])

# the same two seams at the flow level: a wrapper with the new keyword sees the play intent
seen = []
orig_sf, orig_rank = discovery.streams_for, ranking.rank
discovery.streams_for = lambda net, found, type_, video_id, *, budget_s=40.0, intent="bulk": (seen.append(("streams", intent)) or orig_sf(net, found, type_, video_id))
ranking.rank = lambda releases, want, *, batch_binge=None, torrents_only=True, intent="bulk": (seen.append(("rank", intent)) or orig_rank(releases, want, batch_binge=batch_binge, torrents_only=torrents_only))
reset()
serve(HA)
res, _ = call(**base_args(5))
discovery.streams_for, ranking.rank = orig_sf, orig_rank
check("the flow asks for the play intent in both places when the callee can take it, and still plays", ("streams", "play") in seen and ("rank", "play") in seen and res.get("ok") is True)
reset()
res, _ = call(**base_args(5, language="english"))
takes = play_flow._accepts(ranking.normalize_want, "language")
check("a language that the ranking in this build cannot use is reported, one that it can is not",
      res.get("ok") is True and bool(res.get("notes")) == (not takes))

# title cache (contract C4): present, missing, failing
confirmed = []
mod = types.ModuleType(TITLES)
mod.confirm_id = lambda item_id: confirmed.append(item_id)
sys.modules[TITLES] = mod
reset()
res, _ = call(imdb_id=SH.upper(), season=2, episode=5, open=False)
check("the caller's imdb_id is confirmed to the title cache (lower-cased, once)", confirmed == [SH])
confirmed.clear()
res, _ = call(title="Spirit Hunters", season=2, episode=5, open=False)
check("an id found by a title lookup is NOT confirmed (the owner did not choose it from a list)", confirmed == [] and res.get("ok") is True)
res, _ = call(imdb_id="tt1", season=2, episode=5, open=False)
check("a bad imdb_id is refused and not confirmed", res.get("ok") is False and confirmed == [])
mod.confirm_id = lambda item_id: (_ for _ in ()).throw(RuntimeError("cache broke"))
res, _ = call(imdb_id=SH, season=2, episode=5, open=False)
check("a title cache that raises never stops a play", res.get("ok") is True)
sys.modules[TITLES] = None
res, _ = call(imdb_id=SH, season=2, episode=5, open=False)
check("with the title-cache module missing the call is the same", res.get("ok") is True)

# ------------------------------------------------------------------ 10. the poll helper, on a fake clock
class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class Script:
    def __init__(self, create, stats=()):
        self.create_what, self.stats_seq, self.create_timeouts, self.stats_timeouts, self.stats_calls = create, list(stats), [], [], 0

    def create(self, h, trackers=(), timeout=None):
        self.create_timeouts.append(timeout)
        if isinstance(self.create_what, Exception):
            raise self.create_what
        return self.create_what

    def stats(self, h, timeout=None):
        self.stats_calls += 1
        self.stats_timeouts.append(timeout)
        what = self.stats_seq.pop(0) if self.stats_seq else None
        if isinstance(what, Exception):
            raise what
        return what


BUSY = sserver.ServerBusy("held back")
FILES = {"files": [{"path": "a.mkv", "length": 5}]}
NOFILES = {"files": None}

c, s = Clock(), Script(BUSY, [None, None, NOFILES, FILES])
r = sserver.wait_for_files(s, HA, budget_s=15.0, clock=c.now, sleep=c.sleep)
check("poll: files show up at the fourth poll -> 'files', after 2.0 s of short sleeps", r["state"] == "files" and r["waited"] == 2.0 and c.sleeps == [0.5] * 4)
c, s = Clock(), Script(BUSY)
r = sserver.wait_for_files(s, HA, budget_s=15.0, clock=c.now, sleep=c.sleep)
check("poll: a server that never answers -> 'no_answer', and the sleeps never add up past the budget", r["state"] == "no_answer" and sum(c.sleeps) <= 15.0 + 1e-6 and r["waited"] <= 15.2)
check("poll: every call is short (a register call at most CREATE_WAIT_S, a stats call at most POLL_CALL_S)",
      all(t[1] <= sserver.CREATE_WAIT_S for t in s.create_timeouts) and all(t <= sserver.POLL_CALL_S for t in s.stats_timeouts))
c, s = Clock(), Script(NOFILES, [NOFILES])
r = sserver.wait_for_files(s, HA, budget_s=4.0, clock=c.now, sleep=c.sleep)
check("poll: the server answers but lists no files in the whole budget -> 'no_files'", r["state"] == "no_files" and sum(c.sleeps) <= 4.0 + 1e-6)
c, s = Clock(), Script(BUSY, [None])
r = sserver.wait_for_files(s, HA, budget_s=8.0, need_files=False, clock=c.now, sleep=c.sleep)
check("poll: a named file is not waited for: registered, no sleeping, one stats look at most", r["state"] == "registered" and c.sleeps == [] and s.stats_calls <= 1)
c, s = Clock(), Script(FILES)
r = sserver.wait_for_files(s, HA, budget_s=8.0, need_files=False, clock=c.now, sleep=c.sleep)
check("poll: a register answer that already has files is returned as 'files'", r["state"] == "files" and s.stats_calls == 0)
c, s = Clock(), Script(sserver.ServerError("the Stremio server isn't answering at x (is it running? try stack_start)"))
r = sserver.wait_for_files(s, HA, budget_s=8.0, clock=c.now, sleep=c.sleep)
check("poll: a refusal is 'error' with the sentence", r["state"] == "error" and "isn't answering" in r["error"])
c, s = Clock(), Script(BUSY, [BUSY, BUSY, FILES])
r = sserver.wait_for_files(s, HA, budget_s=8.0, clock=c.now, sleep=c.sleep)
check("poll: a stats call that times out is just another poll", r["state"] == "files")
check("the proposed per-release waits are the brief's: 8 s with a file index, 15 s without",
      REAL_BUDGETS[0] == 8.0 and REAL_BUDGETS[1] == 15.0 and play_flow.MAX_TRIES == 3)

# ------------------------------------------------------------------ 11. a chat guest is refused by the handler
reset()
os.environ["JARVIS_CHANNEL_SENDER"] = GUEST
res = play_media.tool_play_episode(base_args(5))
check("a chat guest who calls play_episode anyway is refused in the owner's words: nothing asked, nothing started",
      res.get("ok") is False and "owner" in res["error"] and "failed:" not in res["error"] and AST.log == [] and launched == [] and opened == [])
os.environ["JARVIS_CHANNEL_SENDER"] = "{not json"
check("an unreadable sender counts as a guest", play_media.tool_play_episode(base_args(5)).get("ok") is False and launched == [])
os.environ["JARVIS_CHANNEL_SENDER"] = OWNER
serve(HA)
res = play_media.tool_play_episode(base_args(5))
check("the owner on a chat platform is let through", res.get("ok") is True and launched)
os.environ.pop("JARVIS_CHANNEL_SENDER", None)

# ------------------------------------------------------------------ 12. declarations
schema = next(s for s in play_media.TOOL_SCHEMAS if s["name"] == "play_episode")
props = schema["parameters"]["properties"]
OLD = {"imdb_id", "title", "season", "episode", "type", "item_id", "stream", "infohash", "file_idx", "via", "player", "fullscreen",
       "meta_addon_url", "stream_addon_url", "open"}
NEW = {"quality", "audio", "language", "max_gb", "min_seeders", "pick"}
check("the schema keeps every old argument and adds exactly the contract's six (C2)", OLD <= set(props) and NEW <= set(props) and set(props) == OLD | NEW)
check("pick is an integer, audio is dub / sub / any", props["pick"]["type"] == "integer" and props["audio"]["enum"] == ["dub", "sub", "any"])
check("no tool was added: the file still declares play_episode, play_local, play_control", set(play_media.TOOLS) == {"play_episode", "play_local", "play_control"}
      and len(play_media.TOOL_SCHEMAS) == 3)
entries, _meta, problems = checklist_schema.extract_supplied(play_media.TEST_CHECKLIST, getattr(play_media, "TEST_CHECKLIST_GROUP", None),
                                                             list(play_media.TOOLS), "stremio")
check("the checklist is still well formed", set(entries) == set(play_media.TOOLS) and not problems)
text = json.dumps(play_media.TEST_CHECKLIST["play_episode"])
check("...it has steps for the one-call play, pick and a failure that says why, and still says owner only",
      '"pick": 2' in text and "played: true" in text and "server: down" in text and "Owner only" in text and "OWNER_ONLY_TOOLS" in text
      and "OFFERED" not in text and "guest is offered" not in text)
check("the keywords gained stream phrases in the file's own style", {"stream episode", "start streaming"} <= set(play_media.TOOL_KEYWORDS["play_episode"]))

fake.stop(FAKE)
ASERVER.shutdown()
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
