"""Brief 8C (item N29): the title cache, the duplicate collapse and learning from the owner's pick.

What is real here and what is not:
  * The eight matches of "Rick and Morty" are the REAL list from the owner's event log (finding 04, row 6 of
    results/play-flow-log-175.jsonl), typed in below.  The collapse rule was built from that one list.
  * Everything that talks to an addon talks to the FAKE addon pair in _hub_fake_addon.py (loopback, invented
    wording).  Nothing here ran against a real addon.
  * The clock is injected (title_cache._now); the cache file lives in a temporary HOME.
No network.

Run: python tests/test_hub_title_cache.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-title-cache-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_addon as fake  # noqa: E402
from jarvis.actions import stremio_discovery as tools  # noqa: E402
from jarvis.hub import paths  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import addons as A, config, discovery, title_cache as tc  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


CACHE_FILE = paths.hub_dir() / tc.FILE_NAME
CLOCK = [1_000_000.0]
tc._now = lambda: CLOCK[0]


def reset():
    """An empty cache and the clock back at its start."""
    CLOCK[0] = 1_000_000.0
    try:
        CACHE_FILE.unlink()
    except OSError:
        pass


def file_data():
    """The cache file as data; an empty cache when nothing was ever written."""
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except OSError:
        return {"entries": {}, "offers": []}


# --------------------------------------------------------------------------- the real list (finding 04, row 6)
LOGGED = [
    ("tt2861424", "series", "Rick and Morty", 2013, "imdb", "Cinemeta"),
    ("mf1901739", "series", "Rick and Morty", 2013, "other", "MediaFusion | ElfHosted"),
    ("mf1901738", "series", "Rick and Morty", 2013, "other", "MediaFusion | ElfHosted"),
    ("mf1934563", "series", "Rick and Morty", 2013, "other", "MediaFusion | ElfHosted"),
    ("tt28466465", "series", "Rick and Morty: The Anime", 2024, "imdb", "Cinemeta"),
    ("tt14402464", "series", "Adventures of Rick and morty", 2019, "imdb", "Cinemeta"),
    ("kisskh:6548", "series", "Rick And Morty - Season 1", None, "other", "yastream"),
    ("kisskh:6553", "series", "Rick and Morty - Season 6", None, "other", "yastream"),
]


def result(item_id, type_, name, year, kind, addon, query="Rick and Morty"):
    return {"id": item_id, "type": type_, "name": name, "year": year, "id_kind": kind, "addon": addon,
            "description": "", "images": {}, "score": round(discovery._similarity(query, name), 3)}


def logged(order=None):
    rows = [result(*row) for row in LOGGED]
    return [rows[i] for i in order] if order else rows


def patched_find(results, calls):
    def find(net, found, query, type_=None):
        calls.append(query)
        return {"results": list(results), "searched": 1, "errors": []}
    return find


real_find = discovery.find

# =========================================================================== the collapse (pure)
eight = logged()
check("the eight logged matches ask when nothing is collapsed (the false question of finding 04)", not discovery.is_clear_winner(eight))
folded = tc.collapse(eight)
check("collapse turns the eight into five: the three MediaFusion copies fold into the IMDb one", len(folded) == 5
      and [r["id"] for r in folded] == ["tt2861424", "tt28466465", "tt14402464", "kisskh:6548", "kisskh:6553"])
check("the kept result remembers which ids were folded into it", folded[0]["also"] == ["mf1901739", "mf1901738", "mf1934563"])
check("the logged eight resolve with no question once collapsed", discovery.is_clear_winner(folded) and folded[0]["id"] == "tt2861424")
check("a result with no year is never folded (the two kisskh entries stay)", [r["id"] for r in folded if r["id"].startswith("kisskh")] == ["kisskh:6548", "kisskh:6553"])
check("collapse changes nothing in the list it was given", [r["id"] for r in eight] == [row[0] for row in LOGGED] and "also" not in eight[0])
check("a result nothing folds into comes back as the same object", folded[1] is eight[4])

reordered = tc.collapse(logged([1, 2, 3, 0, 4, 5, 6, 7]))
check("whichever addon answered first, the IMDb one (Cinemeta) is the one kept, in the first member's place",
      reordered[0]["id"] == "tt2861424" and len(reordered) == 5 and discovery.is_clear_winner(reordered))
only_other = tc.collapse([result("mf1", "series", "Foo", 2010, "other", "Zed"), result("mf2", "series", "Foo", 2010, "other", "Cinemeta")])
check("with no IMDb id, Cinemeta is kept; with neither, the first listed", only_other[0]["id"] == "mf2"
      and tc.collapse([result("mf1", "series", "Foo", 2010, "other", "Zed"), result("mf2", "series", "Foo", 2010, "other", "Yan")])[0]["id"] == "mf1")

shows = [result("tt1000001", "series", "Foo", 2013, "imdb", "Cinemeta", "Foo"), result("tt1000002", "series", "Foo", 2019, "imdb", "Cinemeta", "Foo")]
check("two shows with one name and different years do not fold, and still ask", len(tc.collapse(shows)) == 2
      and not discovery.is_clear_winner(tc.collapse(shows)))
check("a missing year on one side does not fold", len(tc.collapse([result("tt1000001", "series", "Foo", 2013, "imdb", "Cinemeta"),
                                                                  result("mf2", "series", "Foo", None, "other", "Zed")])) == 2)
check("a missing year on both sides does not fold", len(tc.collapse([result("mf1", "series", "Foo", None, "other", "A"),
                                                                    result("mf2", "series", "Foo", None, "other", "B")])) == 2)
check("the same name and year with another type does not fold", len(tc.collapse([result("tt1000001", "series", "Foo", 2013, "imdb", "Cinemeta"),
                                                                                result("tt1000003", "movie", "Foo", 2013, "imdb", "Cinemeta")])) == 2)
check("names are compared without case, accents or punctuation", len(tc.collapse([result("tt1000001", "series", "Amélie: The Show!", 2013, "imdb", "Cinemeta"),
                                                                                  result("mf9", "series", "amelie the show", 2013, "other", "Zed")])) == 1)
check("junk in the list is skipped, not fatal", tc.collapse([None, "x", 3, result("tt1000001", "series", "Foo", 2013, "imdb", "Cinemeta")])[0]["id"] == "tt1000001")
check("an empty or missing list collapses to an empty list", tc.collapse([]) == [] and tc.collapse(None) == [])

# =========================================================================== resolve_title (find is replaced by a hand-fed list)
reset()
calls = []
discovery.find = patched_find(logged(), calls)
kind, payload = tools.resolve_title(None, [], "Rick and Morty", "series")
check("resolve_title: the logged eight give ONE answer, tt2861424, with no question", kind == "one" and payload["id"] == "tt2861424")
check("a clear winner is saved", tc.lookup("rick and morty", "series") == {"id": "tt2861424", "type": "series", "name": "Rick and Morty", "year": 2013, "id_kind": "imdb"})

calls.clear()
kind, payload = tools.resolve_title(None, [], "Rick and Morty", "series")
check("resolve_title called as play_episode and bulk_plan call it (4 arguments) still searches: the cache is not read unless asked", calls == ["Rick and Morty"])
calls.clear()
kind, payload = tools.resolve_title(None, [], "Rick and Morty", "series", use_cache=True)
check("with use_cache the saved title answers and discovery.find is not called", kind == "one" and payload["id"] == "tt2861424" and payload.get("cached") is True and not calls)
kind, payload = tools.resolve_title(None, [], "  RICK   and morty! ", "series", use_cache=True)
check("the name is matched without case, spaces or punctuation", kind == "one" and payload.get("cached") is True and not calls)

calls.clear()
kind, payload = tools.resolve_title(None, [], "Rick and Morty", "series", use_cache=True, fresh=True)
check("fresh skips the saved title (find is called) and the new clear winner replaces it", kind == "one" and not payload.get("cached") and calls == ["Rick and Morty"])

# two same-name different-year shows still ask, and nothing about the question is stored
reset()
discovery.find = patched_find(shows, calls)
kind, payload = tools.resolve_title(None, [], "Foo", "series", use_cache=True)
check("two same-name different-year shows still ask", kind == "ask" and [r["id"] for r in payload] == ["tt1000001", "tt1000002"])
check("an \"ask\" answer is never stored", tc.lookup("Foo", "series") is None and not file_data()["entries"])
check("...but the offered ids are remembered", len(file_data()["offers"]) == 1)

# the owner's pick after the question
check("confirm_id with an id that was not offered stores nothing", tc.confirm_id("tt9999999") is False and tc.lookup("Foo", "series") is None)
check("confirm_id with an id that was offered stores name -> that show", tc.confirm_id("tt1000002") is True
      and tc.lookup("Foo", "series")["id"] == "tt1000002")
check("...and the question is closed: the same pick again stores nothing new", tc.confirm_id("tt1000002") is False)
check("the stored pick is what a later lookup (even with no type) returns", tc.lookup("foo", None)["id"] == "tt1000002")

reset()
dup_ask = [result("tt1000001", "series", "Foo", 2013, "imdb", "Cinemeta", "Foo"), result("mf77", "series", "Foo", 2013, "other", "Zed", "Foo"),
           result("tt1000002", "series", "Foo", 2019, "imdb", "Cinemeta", "Foo")]
discovery.find = patched_find(dup_ask, calls)
kind, payload = tools.resolve_title(None, [], "Foo", "series", use_cache=True)
check("a question that still has several after the collapse shows the collapsed list, not the duplicates",
      kind == "ask" and [r["id"] for r in payload] == ["tt1000001", "tt1000002"])
check("the owner picking a hidden duplicate (mf77) is understood as the show it was folded into", tc.confirm_id("mf77") is True
      and tc.lookup("Foo", "series")["id"] == "tt1000001")

reset()
discovery.find = patched_find(shows, calls)
tools.resolve_title(None, [], "Foo", "series")
CLOCK[0] += tc.OFFER_TTL_S + 1
check("a question can only be answered for about 10 minutes", tc.confirm_id("tt1000001") is False and tc.lookup("Foo", "series") is None)
tools.resolve_title(None, [], "Foo", "series")
CLOCK[0] += tc.OFFER_TTL_S - 5
check("...and still can just inside that time", tc.confirm_id("tt1000001") is True)

reset()
discovery.find = patched_find(shows, calls)
tools.resolve_title(None, [], "Foo", "series")
check("an episode id of an offered show counts as the show", tc.confirm_id("tt1000002:1:3") is True and tc.lookup("Foo", "series")["id"] == "tt1000002")

# fresh when the new answer is a question or nothing: the old saved title goes
reset()
discovery.find = patched_find(logged(), calls)
tools.resolve_title(None, [], "Rick and Morty", "series")
discovery.find = patched_find(shows, calls)
kind, _ = tools.resolve_title(None, [], "Rick and Morty", "series", fresh=True)
check("fresh with a lookup that no longer has a clear winner forgets the old saved title", kind == "ask" and tc.lookup("Rick and Morty", "series") is None)
reset()
discovery.find = patched_find(logged(), calls)
tools.resolve_title(None, [], "Rick and Morty", "series")
discovery.find = lambda *a, **k: {"results": [], "searched": 1, "errors": ["nothing"]}
kind, _ = tools.resolve_title(None, [], "Rick and Morty", "series", fresh=True)
check("fresh with a lookup that finds nothing forgets it too, and a failed lookup is never stored", kind == "none" and tc.lookup("Rick and Morty", "series") is None)
kind, _ = tools.resolve_title(None, [], "Anything", "series")
check("a lookup that found nothing stores nothing", kind == "none" and tc.lookup("Anything", "series") is None)

# ids are looked up by their meta, never cached; an id no addon knows drops the titles that pointed at it
reset()
discovery.find = patched_find([result("tt2861424", "series", "Rick and Morty", 2013, "imdb", "Cinemeta", "tt2861424")], calls)
tools.resolve_title(None, [], "tt2861424", "series")
check("an id query is never cached", tc.lookup("tt2861424", "series") is None and not file_data()["entries"])
tc.store("rnm", "series", logged()[0])
tc.store("rick morty", None, logged()[0])
tc.store("other show", "series", result("tt1000009", "series", "Other", 2000, "imdb", "Cinemeta"))
discovery.find = lambda *a, **k: {"results": [], "searched": 1, "errors": ["no addon knows that id"]}
tools.resolve_title(None, [], "tt2861424", "series")
check("\"no addon knows that id\": every saved title pointing at it goes, the others stay", tc.lookup("rnm", "series") is None
      and tc.lookup("rick morty", None) is None and tc.lookup("other show", "series")["id"] == "tt1000009")
discovery.find = real_find

# =========================================================================== the file
reset()
tc.store("alpha", "series", result("tt1000001", "series", "Alpha", 2001, "imdb", "Cinemeta", "alpha"))
entry = list(file_data()["entries"].values())[0]
check("an entry holds only id, type, name, year, id kind and times (no description, image, addon or score)",
      set(entry) <= {"query", "type_key", "id", "type", "name", "year", "id_kind", "stored", "last_used", "uses"}
      and not {"description", "images", "addon", "score"} & set(entry))
tc.store("beta", "series", dict(result("tt1000002", "series", "Beta", 2001, "imdb", "Cinemeta", "beta"), description="SECRET TEXT", images={"poster": "http://x"}))
check("nothing an addon wrote beyond those fields reaches the file", "SECRET TEXT" not in CACHE_FILE.read_text(encoding="utf-8") and "http://x" not in CACHE_FILE.read_text(encoding="utf-8"))
check("no temporary file is left next to it", not [p for p in CACHE_FILE.parent.iterdir() if p.name.endswith(".tmp")])

# lifetime
reset()
tc.store("alpha", "series", result("tt1000001", "series", "Alpha", 2001, "imdb", "Cinemeta", "alpha"))
CLOCK[0] += 89 * 86400
check("a saved title is still good after 89 days", tc.lookup("alpha", "series") is not None)
CLOCK[0] += 2 * 86400
check("...and gone after 90 (N29a)", tc.lookup("alpha", "series") is None)

# uses and last-used
reset()
tc.store("alpha", "series", result("tt1000001", "series", "Alpha", 2001, "imdb", "Cinemeta", "alpha"))
tc.lookup("alpha", "series")
tc.lookup("alpha", "series")
check("how many times an entry was used is counted", list(file_data()["entries"].values())[0]["uses"] == 2)

# types
reset()
tc.store("alpha", None, result("tt1000001", "series", "Alpha", 2001, "imdb", "Cinemeta", "alpha"))
check("a title saved with no type answers a lookup for its own type", tc.lookup("alpha", "series")["id"] == "tt1000001")
check("...but not a lookup for another type", tc.lookup("alpha", "movie") is None)
tc.store("alpha", "movie", result("tt1000002", "movie", "Alpha", 2005, "imdb", "Cinemeta", "alpha"))
check("two saved types of one name: a lookup with no type is a real question, so it misses", tc.lookup("alpha", None) is None)
check("...and each type still answers for itself", tc.lookup("alpha", "movie")["id"] == "tt1000002" and tc.lookup("alpha", "series")["id"] == "tt1000001")
check("forget with no type drops them all", tc.forget("alpha", None) and tc.lookup("alpha", "movie") is None and tc.lookup("alpha", "series") is None)

# the 500-entry bound
reset()
for n in range(520):
    CLOCK[0] += 1
    tc.store("title %d" % n, "series", result("tt%07d" % (1000000 + n), "series", "T%d" % n, 2000, "imdb", "Cinemeta", "x"))
entries = file_data()["entries"]
check("at most 500 entries stay", len(entries) == 500)
check("the oldest went and the newest stayed", tc.lookup("title 0", "series") is None and tc.lookup("title 519", "series") is not None
      and tc.lookup("title 19", "series") is None and tc.lookup("title 20", "series") is not None)
reset()
for n in range(500):
    CLOCK[0] += 1
    tc.store("title %d" % n, "series", result("tt%07d" % (1000000 + n), "series", "T%d" % n, 2000, "imdb", "Cinemeta", "x"))
CLOCK[0] += 1
tc.lookup("title 0", "series")                       # use the oldest one: it is no longer the oldest
CLOCK[0] += 1
tc.store("one more", "series", result("tt2000000", "series", "More", 2000, "imdb", "Cinemeta", "x"))
check("the entry that was used last is not the one that goes", tc.lookup("title 0", "series") is not None and tc.lookup("title 1", "series") is None)

# damaged files
reset()
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
for junk in (b"\x00\xff\xfe garbage", b"", b"[1, 2, 3]", b'"a string"', b'{"entries": 5, "offers": "x"}', b'{"entries": {"k": {"id": 3}}}', b"{" * 50000):
    CACHE_FILE.write_bytes(junk)
    check("a damaged cache file (%r...) is ignored: lookup says nothing, no exception" % junk[:12], tc.lookup("alpha", "series") is None)
    check("...and the next store repairs it", tc.store("alpha", "series", result("tt1000001", "series", "Alpha", 2001, "imdb", "Cinemeta", "alpha"))
          and tc.lookup("alpha", "series")["id"] == "tt1000001")
    CACHE_FILE.write_bytes(junk)
CACHE_FILE.write_text(json.dumps({"version": 1, "offers": [], "entries": {
    "a": {"query": "good", "type_key": "series", "id": "tt1000001", "type": "series", "name": "Good", "year": "2001", "stored": CLOCK[0], "uses": -4},
    "b": {"query": "bad id", "type_key": "series", "id": "../../etc/passwd", "type": "series", "name": "Bad", "stored": CLOCK[0]},
    "c": {"query": "episode id", "type_key": "series", "id": "tt1000001:1:3", "type": "series", "name": "Ep", "stored": CLOCK[0]},
    "d": {"query": "bad type key", "type_key": "../x", "id": "tt1000001", "type": "series", "name": "Bad", "stored": CLOCK[0]},
    "e": {"query": "no name", "type_key": "series", "id": "tt1000001", "type": "series", "stored": CLOCK[0]}}}))
check("entries with a bad id, an episode id, a bad type key or no name are dropped on read; a bad year and uses are cleaned",
      tc.lookup("good", "series") == {"id": "tt1000001", "type": "series", "name": "Good", "year": 2001, "id_kind": "imdb"}
      and tc.lookup("bad id", "series") is None and tc.lookup("episode id", "series") is None
      and tc.lookup("bad type key", "series") is None and tc.lookup("no name", "series") is None)
reset()
blocker = paths.hub_dir() / tc.FILE_NAME
blocker.parent.mkdir(parents=True, exist_ok=True)
blocker.mkdir()                                        # the "file" is a folder: reading and replacing both fail
check("a folder where the file should be: store and lookup both survive", tc.store("a", "series", logged()[0]) is False and tc.lookup("a", "series") is None)
check("...and no temporary file is left behind by the failed write", not [p for p in blocker.parent.iterdir() if p.name.endswith(".tmp")])
blocker.rmdir()

# hostile queries
reset()
nasty = "Rick\x00 and\x1b[31m Morty\r\n\t" + "x" * 5000
check("a 5,000-character query with control characters: store and lookup do not raise, and make one short key",
      tc.store(nasty, "series", logged()[0]) and tc.lookup(nasty, "series")["id"] == "tt2861424"
      and all(len(e["query"]) <= tc.QUERY_LIMIT for e in file_data()["entries"].values()) and CACHE_FILE.stat().st_size < 2000)
check("control characters never reach the stored key", all(ch not in "".join(e["query"] for e in file_data()["entries"].values()) for ch in "\x00\x1b\r\n\t"))
check("a query of nothing but control characters is not stored and finds nothing", tc.store("\x00\x01\x02", "series", logged()[0]) is False
      and tc.lookup("\x00\x01\x02", "series") is None and tc.lookup("", "series") is None and tc.lookup(None, None) is None)
check("a hostile type is treated as no type, not as a path", tc.store("zzz", "../../x", logged()[0]) and tc.lookup("zzz", None) is not None)
check("store refuses a result with no id, no type or no name", not any(tc.store("q", "series", r) for r in (
    {}, None, "x", {"id": "tt1000001"}, {"id": "tt1000001", "type": "series"}, {"id": "a b", "type": "series", "name": "N"}, {"id": "tt1000001:2:3", "type": "series", "name": "N"})))
check("norm_query is stable", tc.norm_query("  Rick  AND morty! ") == "rick and morty" and tc.norm_query("Jujutsu_Kaisen") == "jujutsu kaisen")

# =========================================================================== through the tool, against the fake addons
reset()
BASE, ST, SERVER = fake.start()
A.DEFAULT_META_MANIFEST = "http://127.0.0.1:9/manifest.json"       # never the real Cinemeta
cfg = config.config_path()
cfg.parent.mkdir(parents=True, exist_ok=True)
cfg.write_text(json.dumps({"addons": {"urls": [BASE + "/cat/manifest.json", BASE + "/str/manifest.json"]}}))
Store().set_settings({"allow_private_hosts": ["127.0.0.1"]})

net_calls = []
real_get_json = A.Net.get_json


def counting_get_json(self, url, *args, **kwargs):
    net_calls.append(url)
    return real_get_json(self, url, *args, **kwargs)


A.Net.get_json = counting_get_json

r1 = tools.tool_media_find({"query": "Spirit Hunters"})
first_calls = len(net_calls)
check("media_find the first time asks the addons and resolves", r1.get("resolved") and r1["match"]["id"] == fake.SHOW and first_calls > 0 and "cached" not in r1)
del net_calls[:]
r2 = tools.tool_media_find({"query": "Spirit Hunters"})
check("a second media_find makes NO addon call (counted on Net.get_json, manifests included)", not net_calls)
check("...says cached: true, resolves to the same id and names the same type and year", r2.get("cached") is True and r2["resolved"] is True
      and r2["match"]["id"] == fake.SHOW and r2["match"]["type"] == "series" and r2["match"]["name"] == "Spirit Hunters" and r2["match"]["id_kind"] == "imdb")
check("...and tells the model how to look again", "fresh" in (r2.get("note") or ""))
r3 = tools.tool_media_find({"query": "spirit hunters"})
check("a differently written name hits the same saved title", r3.get("cached") is True and not net_calls)
r4 = tools.tool_media_find({"query": "Spirit Hunters", "type": "series"})
check("naming the type it was resolved to hits it too", r4.get("cached") is True and not net_calls)
r5 = tools.tool_media_find({"query": "Spirit Hunters", "fresh": True})
check("fresh: true skips the saved title and asks the addons again", net_calls and "cached" not in r5 and r5.get("resolved") and r5["match"]["id"] == fake.SHOW)
del net_calls[:]
check("...and the saved title is still there afterwards", tools.tool_media_find({"query": "Spirit Hunters"}).get("cached") is True and not net_calls)
r6 = tools.tool_media_find({"query": "Spirit Hunters", "fresh": "true"})
check("fresh given as the text \"true\" counts too", net_calls and "cached" not in r6)
del net_calls[:]

r = tools.tool_media_find({"query": fake.SHOW})
r_again = tools.tool_media_find({"query": fake.SHOW})
check("an id is looked up each time and never answered from the saved list", r.get("resolved") and "cached" not in r_again and net_calls)
del net_calls[:]

r = tools.tool_media_find({"query": "xyzzy"})
del net_calls[:]
r = tools.tool_media_find({"query": "xyzzy"})
check("a title nothing found is looked up again each time (a failed lookup is never saved)", r["ok"] and r["results"] == [] and net_calls)
del net_calls[:]

r = tools.tool_media_find({"query": "x" * 5000 + "\x00\x1b"})
check("a 5,000-character query through the tool is an answer, not a crash", isinstance(r, dict) and ("results" in r or "needs_clarification" in r or r.get("ok") is False))

# learning from the owner's pick: through media_episodes and stream_search
reset()
r = tools.tool_media_find({"query": "spirit"})
check("a vague name asks (two series)", r.get("needs_clarification") and len(r["matches"]) == 2)
check("...and the question itself is not saved", tc.lookup("spirit", None) is None)
tools.tool_media_episodes({"id": "tt0000000"})
check("an id that was not offered, used in media_episodes, stores nothing", tc.lookup("spirit", None) is None)
tools.tool_media_episodes({"id": fake.SHOW})
saved = tc.lookup("spirit", None)
check("media_episodes with an offered id stores the owner's pick", saved is not None and saved["id"] == fake.SHOW)
del net_calls[:]
r = tools.tool_media_find({"query": "spirit"})
check("the next media_find(\"spirit\") answers with the pick and asks no addon", r.get("resolved") and r.get("cached") and r["match"]["id"] == fake.SHOW and not net_calls)

reset()
r = tools.tool_media_find({"query": "twin"})
check("two equal matches ask", r.get("needs_clarification") and len(r["matches"]) == 2)
tools.tool_stream_search({"id": "tt1", "type": "series", "season": 1, "episode": 1})
saved = tc.lookup("twin", None)
check("stream_search with an offered id stores the pick (even when the search itself finds nothing)", saved is not None and saved["id"] == "tt1" and saved["name"] == "Twin Peaks A")

A.Net.get_json = real_get_json
check("a failing cache never breaks a tool: with the file unreadable, media_find still answers",
      (CACHE_FILE.write_bytes(b"\xff\xfe") or True) and tools.tool_media_find({"query": "Spirit Hunters"}).get("resolved") is True)
check("media_find declares `fresh`, and the cached note tells the model about it",
      "fresh" in next(s for s in tools.TOOL_SCHEMAS if s["name"] == "media_find")["parameters"]["properties"] and "fresh=true" in tools.CACHED_NOTE)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
