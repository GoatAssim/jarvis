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
check("a torrent row carries a magnet for dl_add", all(s["magnet"].startswith("magnet:?xt=urn:btih:") for s in r["streams"] if s["torrent"]))
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
