"""The REAL Cinemeta answer (recorded by tests/addon_checks.py on the owner's machine) run through our code.

tests/data/addon-answers-cinemeta.json is that recording, byte for byte.  It holds ONE real addon, Cinemeta
(Stremio's default list/meta addon), asked for "Jujutsu Kaisen".  What the recording holds, and what this test
therefore checks the code against: the addon's public facts, the five search results (id, type, name, year, images,
in order), the title's seasons, the S1E1 video id, and the one line the stream fan-out said ("no installed addon
offers streams for imdb ids (type series)").

What this test is NOT: the recording has no manifest JSON, no raw responses and no streams.  So the addon is
REBUILT from the recording and served by the loopback fake in _hub_fake_addon.py (at /cin):
  * the catalog list is reconstructed: 8 catalogs (the recording only says "8"), two of them searchable;
  * the response shapes are the protocol's (`releaseInfo` for the year, `videos` with `season`/`episode`);
  * the dates of the episodes and the series' logo URL are invented (the recording only counts them).
Nothing here talked to the real host, v3-cinemeta.strem.io; the fake stands in for it through
`addons.DEFAULT_META_MANIFEST`, so the "default addon" path is the one that runs.  No stream wording exists in the
recording, so nothing here tests how streams are read (markers.py is untouched).

If a future recording holds a non-empty "streams" list, the last section reads each one with ranking.parse_release and
prints how it was read; with none it prints that this part was NOT checked.

Run: python tests/test_hub_stremio_real_cinemeta.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-real-cinemeta-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_addon as fake  # noqa: E402
from jarvis.actions import bulk_download as bulk_tools, play_media, stremio_discovery as tools  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import addons as A, bulk, config, discovery, ranking  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


REC = json.loads((ROOT / "tests" / "data" / "addon-answers-cinemeta.json").read_text(encoding="utf-8"))
REC_ADDON = REC["addons"][0]
SERIES = REC["title"]["id"]
QUERY = "Jujutsu Kaisen"

# ---- rebuild the addon from the recording
CATALOGS = ([{"type": t, "id": "top", "extra": [{"name": "search"}, {"name": "skip"}]} for t in ("movie", "series")]
            + [{"type": t, "id": i, "extra": [{"name": "genre", "isRequired": True}]} for t in ("movie", "series")
               for i in ("year", "imdbRating", "last-videos")])
assert len(CATALOGS) == REC_ADDON["catalogs"]
MANIFEST = {"id": "recorded.cinemeta", "version": REC_ADDON["version"], "name": REC_ADDON["name"], "description": REC_ADDON["description"],
            "types": REC_ADDON["types"], "resources": REC_ADDON["resources"], "catalogs": CATALOGS}


def search_meta(row):
    return dict({"id": row["id"], "type": row["type"], "name": row["name"], "releaseInfo": str(row["year"])}, **row["images"])


def search(type_, query):
    if "jujutsu" not in query:
        return []
    return [search_meta(r) for r in REC["search"] if r["type"] == type_]       # recorded order within a type


def videos():
    out = []
    for s in REC["title"]["seasons"]:
        for n in range(1, s["episodes"] + 1):
            out.append({"id": "%s:%d:%d" % (SERIES, s["season"], n), "season": s["season"], "episode": n,
                        "title": "Episode %d" % n, "released": "2020-01-01T00:00:00.000Z"})    # every recorded episode was released
    return out


TITLE_META = dict(search_meta(next(r for r in REC["search"] if r["id"] == SERIES)), videos=videos(), logo="https://img.invalid/logo.png")
BASE, ST, SERVER = fake.start()
fake.STATE.cin = {"manifest": MANIFEST, "search": search, "metas": {SERIES: TITLE_META}}

cfg = config.config_path()
cfg.parent.mkdir(parents=True, exist_ok=True)
cfg.write_text(json.dumps({}))                         # no account key, no addons.urls: the recorded situation
Store().set_settings({"allow_private_hosts": ["127.0.0.1"]})
A.DEFAULT_META_MANIFEST = BASE + "/cin/manifest.json"   # the fake stands in for Cinemeta's host

net = A.Net(allow=["127.0.0.1"])
found, notes = A.installed(net)

# ---- the addon itself
check("the same two notes the owner's machine printed", notes == REC["notes"])
check("one addon, and it came in as the default", len(found) == 1 and found[0].source == "default")
pub = found[0].public()
for key in ("name", "version", "types", "resources", "description", "streams", "meta", "catalogs"):
    check("addon fact matches the recording: %s" % key, pub[key] == REC_ADDON[key])
check("it can look titles up and cannot give streams", found[0].supports("meta", "series", SERIES) and not found[0].supports("stream", "series", SERIES))

# ---- the search: the recorded five, in the recorded order
answer = discovery.find(net, found, QUERY)
got = [{k: r.get(k) for k in ("id", "type", "name", "year", "id_kind", "addon", "images")} for r in answer["results"][:8]]
check("the search gives exactly the recorded five results, in the recorded order", got == REC["search"])
scores = {r["id"]: r["score"] for r in answer["results"]}
print("   scores:", ", ".join("%s %.3f" % (r["name"][:28], r["score"]) for r in answer["results"]))
check("the series is an exact name match (1.0); the film 'Jujutsu Kaisen 0' is 0.933", scores[SERIES] == 1.0 and abs(scores["tt14331144"] - 0.933) < 0.001)
check("the gap of 0.067 is under the 0.15 the old rule needed", scores[SERIES] - scores["tt14331144"] < 0.15)

# the finding: the series used to lose to its own film; now the exact name wins
check("FINDING FIXED: the exact title is a clear winner although the gap rule alone says ask",
      discovery.is_clear_winner(answer["results"]) and answer["results"][0]["id"] == SERIES)
tied = [dict(r) for r in answer["results"]]
tied[1]["score"] = 1.0
check("two exact matches (two shows with the same name) still ask", not discovery.is_clear_winner(tied))
old_rule = answer["results"][0]["score"] >= 0.9 and answer["results"][0]["score"] - answer["results"][1]["score"] >= 0.15
check("(the gap rule alone, as it was, would have asked)", old_rule is False)
vague = discovery.find(net, found, "Jujutsu")
check("a vaguer query ('Jujutsu') has no exact match and still asks", not discovery.is_clear_winner(vague["results"]))

r = tools.tool_media_find({"query": QUERY})
check("media_find: 'Jujutsu Kaisen' resolves to the series, no question", r["ok"] and r.get("resolved") is True and not r.get("needs_clarification") and r["match"]["id"] == SERIES)
r = tools.tool_media_find({"query": "Jujutsu"})
check("media_find: 'Jujutsu' asks and lists the candidates", r.get("needs_clarification") and r.get("resolved") is False and len(r["matches"]) >= 2)

# ---- the title: seasons, the S1E1 video id
ep = discovery.episodes(net, found, "series", SERIES)
check("title name, id and type match", (ep["title"], ep["id"], ep["type"]) == (REC["title"]["name"], REC["title"]["id"], REC["title"]["type"]))
check("seasons match the recording (season 0 included)", discovery.seasons_of(ep["videos"]) == REC["title"]["seasons"])
check("image fields match the recording", sorted(ep["images"]) == REC["title"]["image_fields"])
check("S1E1 has the recorded video id", next(v for v in ep["videos"] if (v["season"], v["episode"]) == (1, 1))["id"] == REC["video_id"])
check("the latest season is 4, never the specials (season 0)", discovery.latest_season(ep["videos"]) == 4)
r = tools.tool_media_episodes({"id": SERIES, "type": "series"})
check("media_episodes without a season takes season 4 and says so", r["ok"] and r["season"] == 4 and r.get("season_assumed") and len(r["episodes"]) == 1)
print("   media_episodes message:", r.get("note") or r.get("message"))

# ---- the stream fan-out with only Cinemeta
streams = discovery.streams_for(net, found, "series", REC["video_id"])
check("the fan-out says exactly what the recording says", streams["answered"] == REC["answered"] and streams["failed"] == REC["failed"] and streams["streams"] == [])
r = tools.tool_stream_search({"id": SERIES, "type": "series", "season": 1, "episode": 1})
check("stream_search: a plain sentence that no installed addon offers streams (not 'ok' with nothing)",
      r["ok"] is False and "no installed addon offers streams" in r.get("error", "") and not r.get("releases"))
check("the sentence names no address and no key", "127.0.0.1" not in json.dumps(r) and "cin/manifest" not in json.dumps(r))
check("stream_search made no stream request", not any("/stream/" in x for x in ST.log))
plans_before = list(bulk.plans_dir().glob("*"))
r = bulk_tools.tool_bulk_plan({"title": QUERY})
check("bulk_plan: the same sentence, no plan_id, no 'next' telling the model to bulk_run",
      r["ok"] is False and "no installed addon offers streams" in r.get("error", "") and "plan_id" not in r and "next" not in r)
r2 = bulk_tools.tool_bulk_plan({"id": SERIES, "type": "series", "season": 4})
check("bulk_plan with the id and season: the same", r2["ok"] is False and "no installed addon offers streams" in r2.get("error", "") and "plan_id" not in r2)
check("no plan was saved", list(bulk.plans_dir().glob("*")) == plans_before)
check("bulk_run with a made-up plan id queues nothing", bulk_tools.tool_bulk_run({"plan_id": "p-nothing"})["ok"] is False)

# ---- play_episode by title with only Cinemeta (tier 1 needs no streams)
res = play_media.tool_play_episode({"title": QUERY, "season": 1, "episode": 1, "open": False})
check("play_episode by title with real Cinemeta: one clear match, the episode page for the series id",
      res.get("ok") and res.get("tier") == 1 and res.get("link", "").endswith("#/detail/series/%s/%s:1:1" % (SERIES, SERIES)) and QUERY in res.get("matched", ""))
res = play_media.tool_play_episode({"title": "Jujutsu", "season": 1, "episode": 1, "open": False})
check("play_episode by 'Jujutsu' asks which one", res.get("needs_clarification") and {m["id"] for m in res.get("matches", [])} >= {SERIES, "tt14331144"})

# ---- what bulk_plan says for the one-episode season 4, once an addon DOES offer streams (the fake stream addon)
cfg.write_text(json.dumps({"addons": {"urls": [BASE + "/str/manifest.json"]}}))
plan = bulk_tools.tool_bulk_plan({"title": QUERY})
check("bulk_plan without a season, with a stream addon: takes season 4 and says it assumed", plan.get("ok") and plan.get("season") == 4 and plan.get("season_assumed"))
print("   bulk_plan message (fake stream addon, season never named):")
print("      " + str(plan.get("message")).replace("\n", "\n      "))
cfg.write_text(json.dumps({}))

# ---- streams: nothing recorded, so say so
if REC["streams"]:
    print("\nhow the table read each recorded stream:")
    for row in REC["streams"]:
        rel = ranking.parse_release({"name": row["text"], "title": row["text"], "behaviorHints": {"filename": row["filename"]}})
        print("   %-8s %s" % (rel["resolution"] or "?", row["text"].replace("\n", " ")[:90]))
    check("(streams were recorded: see the table above; compare it with 'read_as' by eye)", True)
else:
    print("\nNOT CHECKED: the recording holds no streams (the owner has no addon that offers them), so how real stream text is read is untested.")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
