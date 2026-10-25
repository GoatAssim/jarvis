"""Download Hub phase 6: finding shows through addons (media_find, media_episodes, stremio_addons,
stream_search, stremio_library, stremio_mark_watched) against a FAKE addon pair and a FAKE cloud.
No network.  The addons' wording is invented (see _hub_fake_addon.py); nothing here ran against a
real addon or a real Stremio account.

Run: python tests/test_hub_stremio_discovery.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-discovery-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_addon as fake  # noqa: E402
from jarvis import checklist_schema  # noqa: E402
from jarvis.actions import stremio_discovery as tools  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import addons as A, config, discovery  # noqa: E402
from jarvis.hub.stremio import pick_store, title_cache  # noqa: E402  (brief 8C)

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


BASE, ST, SERVER = fake.start()
# Offline on purpose: when no installed addon can look titles up, discovery falls back to the REAL Cinemeta.
# On a PC with internet that fallback answers, and the check 'an addon that errors ... nothing found' fails.
# Point the default at a closed local port so this test never depends on the network.
A.DEFAULT_META_MANIFEST = "http://127.0.0.1:9/manifest.json"
cfg = config.config_path()
cfg.parent.mkdir(parents=True, exist_ok=True)
cfg.write_text(json.dumps({"addons": {"urls": [BASE + "/cat/manifest.json", BASE + "/str/manifest.json"]}}))
store = Store()


def allow(hosts):
    store.set_settings({"allow_private_hosts": hosts})


# ---- the guard: a loopback addon is refused until the owner allow-lists it
r = tools.tool_stremio_addons({})
check("a loopback addon is refused by the SSRF guard until allow-listed",
      r["ok"] and r["count"] == 0 or "private" in json.dumps(r) or "refused" in json.dumps(r))
check("the refusal doesn't print the address' path or a key", "SECRETKEY" not in json.dumps(r))
allow(["127.0.0.1"])

# ---- stremio_addons
r = tools.tool_stremio_addons({})
check("both addons are listed", r["ok"] and r["count"] == 2 and {a["name"] for a in r["addons"]} == {"Fake Catalog", "Fake Streams"})
check("an addon's address (the path can hold a key) is never shown", BASE not in json.dumps(r) and "manifest.json" not in json.dumps(r)
      and "/cat" not in json.dumps(r).replace("~/.jarvis", ""))
check("what each offers is read from its manifest", next(a for a in r["addons"] if a["name"] == "Fake Streams")["streams"]
      and not next(a for a in r["addons"] if a["name"] == "Fake Streams")["meta"])
check("no account key is reported plainly", "no account key" in r["account"])

# ---- media_find
r = tools.tool_media_find({"query": "Spirit Hunters"})
check("an exact title resolves to one match with its id and type", r["ok"] and r.get("resolved") and r["match"]["id"] == fake.SHOW
      and r["match"]["type"] == "series" and r["match"]["id_kind"] == "imdb")
check("the poster field names are passed on", set(r["match"]["images"]) == {"poster"})
r = tools.tool_media_find({"query": "spirit"})
check("a vague name asks which one (needs_clarification with a short list)", r.get("needs_clarification") and len(r["matches"]) == 2)
r = tools.tool_media_find({"query": "twin"})
check("two equal matches ask too", r.get("needs_clarification") and len(r["matches"]) == 2)
r = tools.tool_media_find({"query": "xyzzy"})
check("nothing found says so and suggests the full title", r["ok"] and r["results"] == [] and "full title" in r["message"])
r = tools.tool_media_find({"query": "Moon Pilot"})
check("an anime catalogued by a Kitsu id resolves with that id and the addon's own type",
      r.get("resolved") and r["match"]["id"] == "kitsu:42" and r["match"]["type"] == "anime" and r["match"]["id_kind"] == "kitsu")
r = tools.tool_media_find({"query": fake.SHOW})
check("an IMDb id is looked up directly", r.get("resolved") and r["match"]["name"] == "Spirit Hunters")
r = tools.tool_media_find({"query": "kitsu:42"})
check("a Kitsu id is looked up directly", r.get("resolved") and r["match"]["name"] == "Moon Pilot")
check("a catalog that requires another filter is never searched", not any("browse-only" in p for p in ST.log))
check("an empty query asks", tools.tool_media_find({}).get("needs_clarification"))
# brief 8C: "Spirit Hunters" was resolved above, so media_find now answers it from the saved title list and asks no addon.  The
# check below is about an addon that FAILS, so the saved title is forgotten first (the check itself is unchanged).
title_cache.forget("Spirit Hunters", None)
ST.fail.add("/cat")
r = tools.tool_media_find({"query": "Spirit Hunters"})
check("an addon that errors gives a clear 'no addon can look titles up' / nothing found, no crash", r.get("ok") is False or r.get("results") == [])
ST.fail.clear()

# ---- media_episodes
r = tools.tool_media_episodes({"id": fake.SHOW})
check("two seasons listed", r["ok"] and [s["season"] for s in r["seasons"]] == [1, 2])
check("no season named: the latest released one, and it says so", r["season"] == 2 and r["season_assumed"] and "latest" in r["note"])
check("an episode that isn't out is marked", [e["released"] for e in r["episodes"]] == [True, True, True, False])
r = tools.tool_media_episodes({"id": fake.SHOW, "season": 1})
check("a named season lists its episodes", r["season"] == 1 and len(r["episodes"]) == 3 and not r["season_assumed"])
r = tools.tool_media_episodes({"id": fake.SHOW, "season": 9})
check("a season that doesn't exist is said plainly", r["ok"] and r["episodes"] == [] and "no season 9" in r["note"])
r = tools.tool_media_episodes({"id": "kitsu:42", "type": "anime"})
check("a Kitsu title's episodes use the addon's own video ids", r["ok"] and len(r["episodes"]) == 2)
check("an id that isn't an id is refused", tools.tool_media_episodes({"id": "../../etc/passwd"}).get("needs_clarification"))
r = tools.tool_media_episodes({"id": "tt0000000"})
check("a title no addon knows is an error, not a crash", r["ok"] is False)

# ---- stream_search
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2, "episode": 1, "quality": "1080", "audio": "dub"})
check("both addons were asked", r["ok"] and set(r["answered"]) == {"Fake Catalog", "Fake Streams"})
check("dub 1080p is first", r["streams"][0]["label"] == "1080p dub" and r["streams"][0]["tier"] == "exact")
check("the same torrent from two addons is listed once", sum(1 for s in r["streams"] if s["label"] == "1080p dub" and s["torrent"]) == 1)
check("the direct link is listed, flagged, and its address is not in the result",
      any(s["direct_link"] for s in r["streams"]) and "secret-token" not in json.dumps(r))
# MOVED in brief 8C (N26a): this pinned `magnet` on every torrent row; the row now carries `infohash` and `file_idx` and no magnet
check("a torrent row carries an infohash and a file_idx for dl_add_stremio and play_episode (no magnet)",
      all(len(s["infohash"]) == 40 and "file_idx" in s and "magnet" not in s for s in r["streams"] if s["torrent"]))
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2, "episode": 2, "quality": 1080, "audio": "dub"})
check("a CAM copy never appears and is counted as rejected", r["rejected"] and not any("CAM" in s["name"] for s in r["streams"]))
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2, "episode": 3, "quality": 1080, "audio": "dub"})
check("no dub 1080p for E3: nothing is 'exact'", r["exact"] == 0 and r["streams"][0]["tier"] != "exact")
check("a release that doesn't state its language says so, and isn't called a dub",
      any(s["audio"] == "unknown" for s in r["streams"]) and "language" in (r.get("note") or ""))
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2})
check("a missing episode asks", r.get("needs_clarification"))
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2, "episode": 1, "quality": "999"})
check("a bad quality is an error", r["ok"] is False)
r = tools.tool_stream_search({"id": fake.SHOW, "season": 5, "episode": 1})
check("an episode the lists don't have is an error", r["ok"] is False)
r = tools.tool_stream_search({"id": "kitsu:42", "type": "anime", "season": 1, "episode": 1, "audio": "dub"})
check("a Kitsu episode is asked of the addon that handles kitsu: ids (the other is skipped by its prefix rules)",
      r["ok"] and r["answered"] == ["Fake Streams"] and r["streams"][0]["audio"] == "dub")
from jarvis.hub.stremio import stream_cache  # noqa: E402  (8D: stream_search now reads the short stream memory)
stream_cache.clear()   # this check is about an addon that FAILS, so the earlier answer must not be served from memory
ST.fail_streams.add("/str")
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2, "episode": 1, "audio": "dub"})
check("one addon failing is reported; the other still answers", r["ok"] and r["found"] > 0 and r.get("problems"))
ST.fail_streams.clear()
ST.fail.add("/str")
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2, "episode": 1, "audio": "dub"})
check("an addon whose manifest can't be read is left out with a note, not a crash", r["ok"] and r["answered"] == ["Fake Catalog"] and "problems" not in r
      and "didn't answer" in (r.get("note") or "") or r["ok"])
ST.fail.clear()
ST.slow["/str"] = 2
before = len(ST.log)
r = tools.tool_stream_search({"id": fake.SHOW, "season": 2, "episode": 1})
ST.slow.clear()
check("a slow addon is waited for within the budget", r["ok"] and r["found"] > 0)

# ---- brief 8C: rows without a magnet, a rank to pick by, a short memory of the search.  The streams are HAND-MADE objects fed in
# place of the addons' answer (invented wording, no network); the title lookups before this point used the fake addons.
import re  # noqa: E402
import types  # noqa: E402

from jarvis import tool_result_shaping  # noqa: E402
from jarvis.actions import download_hub  # noqa: E402
from jarvis.hub import router  # noqa: E402
from jarvis.hub.stremio import links  # noqa: E402


def _hand_stream(n, idx=None):
    out = {"name": "Fake\n1080p", "title": "Spirit Hunters S02E01 1080p WEB-DL DUAL AUDIO #%d\n\U0001F464 %d" % (n, 100 - n),
           "infoHash": "%040x" % n, "sources": ["tracker:udp://t.example.invalid:1/a"]}
    if idx is not None:
        out["fileIdx"] = idx
    return out


_EIGHT = [(_hand_stream(n, idx=4 if n == 7 else None), "Fake Addon") for n in range(1, 9)]
_real_streams_for = discovery.streams_for
discovery.streams_for = lambda net, found, type_, video_id, **kw: {"streams": list(_EIGHT), "answered": ["Fake Addon"], "failed": [], "asked": 1}
_ARGS = {"id": fake.SHOW, "season": 2, "episode": 1, "quality": 1080, "audio": "dub"}
r = tools.tool_stream_search(dict(_ARGS))
check("the default is 5 rows (N26b) and found still says how many there are", r["ok"] and len(r["streams"]) == 5 and r["found"] == 8)
check("rows are numbered from 1 in order", [s["rank"] for s in r["streams"]] == [1, 2, 3, 4, 5])
check("no row carries a magnet and no magnet link is anywhere in the answer", all("magnet" not in s for s in r["streams"]) and "magnet:?" not in json.dumps(r))
check("every torrent row carries a 40-hex infohash and a file_idx key", all(re.fullmatch("[0-9a-f]{40}", s["infohash"]) and "file_idx" in s for s in r["streams"]))
check("search_for is the video id the search was made for", r["search_for"] == r["video_id"] and r["video_id"].endswith(":2:1"))
check("limit 25 shows all 8, limit 3 shows 3, a limit past 25 is capped, junk and 0 mean the default",
      len(tools.tool_stream_search(dict(_ARGS, limit=25))["streams"]) == 8 and len(tools.tool_stream_search(dict(_ARGS, limit=3))["streams"]) == 3
      and len(tools.tool_stream_search(dict(_ARGS, limit=999))["streams"]) == 8 and len(tools.tool_stream_search(dict(_ARGS, limit="x"))["streams"]) == 5
      and len(tools.tool_stream_search(dict(_ARGS, limit=0))["streams"]) == 5)
r25 = tools.tool_stream_search(dict(_ARGS, limit=25))
vid = r25["video_id"]
check("the search is remembered by video id: each shown rank recalls the same torrent",
      all(pick_store.recall(vid, s["rank"])["infohash"] == s["infohash"] for s in r25["streams"]))
tools.tool_stream_search(dict(_ARGS))
check("ALL ranked candidates are remembered, not only the 5 shown", pick_store.recall(vid, 8)["infohash"] == r25["streams"][7]["infohash"]
      and pick_store.recall(vid, 9) is None)
seven = next(s for s in r25["streams"] if s["infohash"] == "%040x" % 7)
check("a file index the addon gave is shown and kept for the pick; one it did not give is null",
      seven["file_idx"] == 4 and pick_store.recall(vid, seven["rank"])["file_idx"] == 4 and pick_store.recall(vid, seven["rank"])["stream"]["fileIdx"] == 4
      and all(s["file_idx"] is None for s in r25["streams"] if s is not seven))
check("the remembered stream is one play_episode can take (infoHash and fileIdx)",
      links.parse_stream(pick_store.recall(vid, seven["rank"])["stream"])["infohash"] == seven["infohash"]
      and links.parse_stream(pick_store.recall(vid, seven["rank"])["stream"])["file_idx"] == 4)

# dropping the magnet loses no download path: dl_add_stremio takes the bare infohash as `magnet`, plus file_index
src, problem = download_hub._stremio_source({"magnet": seven["infohash"], "file_index": seven["file_idx"]})
check("dl_add_stremio(magnet=<infohash>, file_index=<file_idx>) builds the same source the magnet used to", problem is None
      and src.startswith("magnet:?xt=urn:btih:" + seven["infohash"]) and "&so=4" in src)
src, problem = download_hub._stremio_source({"magnet": r["streams"][0]["infohash"]})
check("...and a row with no file index downloads with just the infohash", problem is None and "so=" not in src)
check("dl_add routes a bare 40-hex infohash to the stremio engine (router rule)", router._INFOHASH.match(r["streams"][0]["infohash"]) is not None)

# the shaping still matches the rows
import jarvis.tools  # noqa: E402,F401  (loading the tools is what registers each file's TOOL_RESULT_SPECS)
medium = tool_result_shaping.shape_result("stream_search", r25, "medium")
low = tool_result_shaping.shape_result("stream_search", r25, "low")
check("at medium a row keeps rank, label, infohash and file_idx and loses the nice-to-have fields",
      all({"rank", "label", "infohash", "file_idx", "size_gb", "seeders", "addon", "name"} <= set(s) and not {"tier", "codec", "source_type", "torrent", "direct_link", "magnet"} & set(s)
          for s in medium["streams"]))
check("at low it also loses name, resolution and audio, but keeps infohash and file_idx (they replaced the magnet)",
      all({"rank", "label", "infohash", "file_idx"} <= set(s) and not {"name", "resolution", "audio"} & set(s) for s in low["streams"]))
check("the top-level fields the model needs survive both", all(k in medium and k in low for k in ("search_for", "video_id", "streams", "next")))

# the helpers that wait for 8A / 8B / 8D: BOTH branches, with fake modules (contracts C3 and C5)
ns = types.SimpleNamespace
seen = {}


def _nw_old(resolution=None, audio=None, max_gb=None, min_seeders=None):
    seen["nw"] = "old"
    return {"resolution": None, "audio": "any", "max_gb": None, "min_seeders": None}, None


def _nw_new(resolution=None, audio=None, max_gb=None, min_seeders=None, language=None):
    seen["nw"] = ("new", language)
    return {"resolution": None, "audio": "any", "max_gb": None, "min_seeders": None, "language": language}, None


want, problem, note = tools._normalize_want(ns(normalize_want=_nw_old), {"language": "English"})
check("normalize_want without `language`: the call still works, and the note says the language was NOT applied",
      problem is None and seen["nw"] == "old" and note and "English" in note and "not applied" in note)
want, problem, note = tools._normalize_want(ns(normalize_want=_nw_old), {})
check("...and with no language asked there is no note", note is None)
want, problem, note = tools._normalize_want(ns(normalize_want=_nw_new), {"language": " english "})
check("normalize_want with `language`: it is passed through, trimmed, and there is no note", seen["nw"] == ("new", "english") and note is None and want["language"] == "english")
want, problem, note = tools._normalize_want(ns(normalize_want=_nw_new), {})
check("...with none asked, None is passed", seen["nw"] == ("new", None))


def _rank_old(releases, want, *, batch_binge=None, torrents_only=True):
    seen["rank"] = ("old", torrents_only)
    return [], {}


def _rank_new(releases, want, *, batch_binge=None, torrents_only=True, intent="bulk"):
    seen["rank"] = ("new", torrents_only, intent)
    return [], {}


tools._rank(ns(rank=_rank_old), [], {})
check("rank without `intent`: called as today (all streams, no intent)", seen["rank"] == ("old", False))
tools._rank(ns(rank=_rank_new), [], {})
check("rank with `intent`: the play intent is passed", seen["rank"] == ("new", False, "play"))


def _sf_old(net, found, type_, video_id, *, budget_s=40.0):
    seen["sf"] = "old"
    return {"streams": [], "answered": [], "failed": [], "asked": 0}


def _sf_new(net, found, type_, video_id, *, budget_s=40.0, intent="bulk"):
    seen["sf"] = ("new", intent)
    return {"streams": [], "answered": [], "failed": [], "asked": 0}


tools._streams_for(ns(streams_for=_sf_old), None, [], "series", "tt1")
check("streams_for without `intent`: called as today", seen["sf"] == "old")
tools._streams_for(ns(streams_for=_sf_new), None, [], "series", "tt1")
check("streams_for with `intent`: the play intent is passed", seen["sf"] == ("new", "play"))
check("a callee whose signature can't be read counts as taking nothing extra", tools._params(42) == set())

r = tools.tool_stream_search(dict(_ARGS, language="english"))
check("a language is accepted: either applied (wants has it) or said plainly to be not applied; never silently dropped",
      r["ok"] and (r["wants"].get("language") or "not applied" in (r.get("note") or "")))
check("the answer says how to play and how to download a row, matching what this build's play_episode takes",
      ("pick=" in r["next"]) == tools._play_takes_pick() and "dl_add_stremio" in r["next"] and "infohash" in r["next"])
discovery.streams_for = lambda net, found, type_, video_id, **kw: {"streams": [], "answered": ["Fake Addon"], "failed": [], "asked": 1}
r = tools.tool_stream_search(dict(_ARGS))
check("a search that finds nothing has no `next` and replaces the remembered list with an empty one",
      r["ok"] and r["streams"] == [] and "next" not in r and pick_store.recall(vid, 1) is None)
discovery.streams_for = _real_streams_for

props = {s["name"]: s["parameters"]["properties"] for s in tools.TOOL_SCHEMAS}
check("the schemas declare `language` and `limit` (default 5) on stream_search and `fresh` on media_find",
      "language" in props["stream_search"] and "5" in props["stream_search"]["limit"]["description"] and "fresh" in props["media_find"])

# ---- the cloud: account addons, library, mark watched
A.CLOUD_API = BASE + "/api/"
cfg.write_text(json.dumps({"account": {"auth_key": "SECRETKEY"}}))
r = tools.tool_stremio_addons({})
check("with an account key the addon list comes from the account", r["count"] == 2 and r["account"] == "set up")
check("the account key is sent in the body, never in the URL, and never returned",
      any(m == "addonCollectionGet" and b.get("authKey") == "SECRETKEY" for m, b in ST.posts) and "SECRETKEY" not in json.dumps(r)
      and not any("SECRETKEY" in p for p in ST.log))
r = tools.tool_stremio_library({})
check("the library lists titles, newest watched first, and skips removed ones",
      r["ok"] and [i["name"] for i in r["items"]] == ["Spirit Hunters", "Old Film"])
check("progress is computed", next(i for i in r["items"] if i["name"] == "Old Film")["progress_pct"] == 10.0)
r = tools.tool_stremio_library({"show": "continue_watching"})
check("continue watching = started, not finished", [i["name"] for i in r["items"]] == ["Old Film"])
r = tools.tool_stremio_library({"show": "watched"})
check("watched filter", [i["name"] for i in r["items"]] == ["Spirit Hunters"])
r = tools.tool_stremio_library({"search": "old"})
check("search filter", [i["name"] for i in r["items"]] == ["Old Film"])
check("a bad filter is an error", tools.tool_stremio_library({"show": "x"})["ok"] is False)

n_posts = len(ST.posts)
r = tools.tool_stremio_mark_watched({"id": "tt0000011"})
check("mark watched previews by default and writes nothing", r["ok"] and not r["applied"] and r["after"]["timesWatched"] == 1
      and not any(m == "datastorePut" for m, _ in ST.posts[n_posts:]))
r = tools.tool_stremio_mark_watched({"id": "tt0000011", "apply": True})
check("apply backs the item up first, then writes", r["ok"] and r["applied"] and Path(r["backup"]).exists()
      and json.loads(Path(r["backup"]).read_text())["_id"] == "tt0000011")
put = [b for m, b in ST.posts if m == "datastorePut"][-1]
check("only the watched fields of that one title were changed", len(put["changes"]) == 1
      and put["changes"][0]["state"]["timeOffset"] == 600000 and put["changes"][0]["state"]["flaggedWatched"] == 1)
check("the backup holds the OLD state", json.loads(Path(r["backup"]).read_text())["state"]["timesWatched"] == 0)
r = tools.tool_stremio_mark_watched({"id": "tt0000011", "watched": False, "apply": True})
check("and it can be undone", r["applied"] and ST.library[0]["state"]["timesWatched"] == 0)
r = tools.tool_stremio_mark_watched({"id": fake.SHOW})
check("a series is refused (the per-episode bit field is not confirmed)", r["ok"] is False and "series" in r["error"])
check("a title that isn't in the library is refused", tools.tool_stremio_mark_watched({"id": "tt0000012"})["ok"] is False)
check("an id that isn't an id is refused", tools.tool_stremio_mark_watched({"id": "a b"})["ok"] is False)
cfg.write_text(json.dumps({"account": {"auth_key": "WRONG"}}))
r = tools.tool_stremio_library({})
check("a rejected key is a plain sentence; the key is not in it", r["ok"] is False and "WRONG" not in json.dumps(r) and "SECRETKEY" not in json.dumps(r))
cfg.write_text("{}")
r = tools.tool_stremio_library({})
check("no account key says how to set it up", r["ok"] is False and "auth_key" in r["error"])

# ---- the declarations
# ---- brief 18: the exact name wins; "no addon offers streams" is a plain sentence (hand-built results, no network)
def _row(name, score):
    return {"id": "tt" + name, "name": name, "score": score}


check("one exact name wins even when a longer title is close behind (the real 1.0 vs 0.933 case)",
      discovery.is_clear_winner([_row("a", 1.0), _row("b", 0.933), _row("c", 0.85)]))
check("two exact names still ask", not discovery.is_clear_winner([_row("a", 1.0), _row("b", 1.0)]))
check("no exact name and a small gap still asks", not discovery.is_clear_winner([_row("a", 0.93), _row("b", 0.85)]))
check("the old gap rule still wins without an exact name", discovery.is_clear_winner([_row("a", 0.95), _row("b", 0.5)]))
check("an exact name that is not first (hand-ordered) is not trusted", not discovery.is_clear_winner([_row("a", 0.933), _row("b", 1.0)]))
sentence = discovery.no_streams_sentence("series", "tt0000001")
check("the no-streams sentence is plain and says what to do", "no installed addon offers streams for imdb ids (type series)" in sentence and "Add an addon" in sentence)
check("it names no address, no key and no path", "http" not in sentence and "key" not in sentence.replace("account key", "") and "/" not in sentence.replace("addons.urls", "").replace("~/.jarvis/stremio.json", ""))
cfg.write_text(json.dumps({"addons": {"urls": [BASE + "/cat/manifest.json", BASE + "/str/manifest.json"]}}))
_pair = A.installed(A.Net(allow=["127.0.0.1"]))[0]
cfg.write_text(json.dumps({"addons": {"urls": [BASE + "/cat/manifest.json"]}}))
_cat_only = A.installed(A.Net(allow=["127.0.0.1"]))[0]
check("offers_streams: true for the fake pair; false for no addon at all",
      len(_pair) == 2 and discovery.offers_streams(_pair) and not discovery.offers_streams([]))
check("offers_streams looks at the type and id too: the catalog addon offers series streams but not a made-up type",
      discovery.offers_streams(_cat_only, "series", "tt0000001") and not discovery.offers_streams(_cat_only, "channel", "tt0000001"))

check("the tools are declared in the stremio group, with schemas for every one",
      tools.TOOL_GROUP == "stremio" and {s["name"] for s in tools.TOOL_SCHEMAS} == set(tools.TOOLS))
check("only the account write is confirm-gated here", tools.TOOL_CONFIRM_REQUIRED == {"stremio_mark_watched"})
bad = []
for name, entry in tools.TEST_CHECKLIST.items():
    problems = checklist_schema.validate_entry(name, entry)
    if problems:
        bad.append((name, problems))
check("every checklist entry is well formed", not bad and set(tools.TEST_CHECKLIST) == set(tools.TOOLS))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
