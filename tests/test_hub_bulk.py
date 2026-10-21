"""Download Hub phase 6, the acceptance scenario (N13): "download me the whole season of <show> using
stremio dub 1080p" against a FAKE addon pair (two seasons, dubbed / subbed, 720p / 1080p releases)
and the FAKE Stremio server.  No network.

Proves: the show is resolved (and asked about when ambiguous), the latest season is taken and said
so, every episode gets a dubbed 1080p pick, the episode with no dubbed 1080p is listed with what
exists instead and is NOT queued until the owner chooses, nothing is queued by bulk_plan, bulk_run
makes ONE group, a rerun / restart duplicates nothing and skips finished episodes, a release nobody
seeds is replaced by the next one by the real Stremio engine, caps and the disk check hold.

The wording the addons use is invented.  Nothing here ran against a real addon or Stremio server.

Run: python tests/test_hub_bulk.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-bulk-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_addon as fakeaddon  # noqa: E402
import _hub_fake_stremio as fakesrv  # noqa: E402
import _hub_testserver as srv  # noqa: E402
from jarvis import checklist_schema  # noqa: E402
from jarvis.actions import bulk_download as tools  # noqa: E402
from jarvis.hub import states  # noqa: E402
from jarvis.hub.engines import stremio as seng  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import bulk, config, links  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


BASE, AST, _SERVER = fakeaddon.start()
FAKE, SBASE, SST = fakesrv.start()
os.environ["JARVIS_STREMIO_SERVER_URL"] = SBASE
cfg = config.config_path()
cfg.parent.mkdir(parents=True, exist_ok=True)
cfg.write_text(json.dumps({"addons": {"urls": [BASE + "/cat/manifest.json", BASE + "/str/manifest.json"]}}))
LIB = Path(HOME) / "library"
store = Store()
store.set_settings({"allow_private_hosts": ["127.0.0.1"], "library_root": str(LIB), "stremio.stall_seconds": 10,
                    "stremio.metadata_timeout": 10, "http.stall_seconds": 3})
SH = fakeaddon.SHOW


def rows():
    return store.list_items(limit=500)


# ---- 1. the owner's sentence, as a plan
plan = tools.tool_bulk_plan({"title": "Spirit Hunters", "quality": "1080p", "audio": "dub"})
check("the show is resolved from its name and a plan comes back", plan["ok"] and plan["title"] == "Spirit Hunters" and plan["plan_id"].startswith("bp"))
check("no season named: the latest is taken, and the plan says so", plan["season"] == 2 and plan["season_assumed"] and "latest" in plan["message"])
check("bulk_plan queues NOTHING", rows() == [] and not store.list_groups())
tot = plan["totals"]
check("E1 and E2 have an exact dubbed 1080p pick", tot["exact"] == 2 and [e["status"] for e in plan["episodes"][:2]] == ["exact", "exact"])
e1, e2, e3, e4 = plan["episodes"]
check("E1's pick is the dub at 1080p (not the 300-seeder sub)", e1["pick"]["label"] == "1080p dub" and e1["pick"]["seeders"] == 120)
check("E2's pick is a 1080p dub and it keeps a fallback", e2["pick"]["label"] == "1080p dub" and e2["fallbacks"] == 1)
saved = json.loads((Path(HOME) / ".jarvis" / "hub" / "bulk_plans" / (plan["plan_id"] + ".json")).read_text())
check("the CAM copy (900 seeders) was never considered, and the plan file says why", e2["pick"]["seeders"] != 900
      and "banned token 'CAM'" in saved["episodes"][1]["rejected"])
check("E3 has no dubbed 1080p: it is pending, with what exists listed",
      e3["status"] == "pending" and {o["policy"] for o in e3["options"]} == {"other_quality", "other_audio"} and "no dubbed 1080p" in e3["note"])
check("E4 is not out yet and is not planned", e4["status"] == "unreleased")
check("the plan says what to ask, in words", "Episode 3" in plan["message"] and "720p dub" in plan["message"] and "Nothing is queued yet" in plan["message"])
check("the plan has a size and a destination", plan["totals"]["size_gb"] > 1 and plan["destination"].endswith("Stremio/Spirit Hunters/Season 02")
      and plan["destination"].startswith(str(LIB).replace("\\", "/")))
check("no magnet or addon address is in the model-facing plan", "magnet:" not in json.dumps(plan) and BASE not in json.dumps(plan))
check("the plan is kept on disk under its id", (Path(HOME) / ".jarvis" / "hub" / "bulk_plans" / (plan["plan_id"] + ".json")).exists())

# ---- 2. ambiguity and errors are asked, not guessed
r = tools.tool_bulk_plan({"title": "Spirit"})
check("an ambiguous title asks which one", r.get("needs_clarification") and len(r["matches"]) == 2)
r = tools.tool_bulk_plan({"title": "Twin"})
check("equal matches ask too", r.get("needs_clarification"))
r = tools.tool_bulk_plan({"title": "xyzzy"})
check("an unknown title says so", r["ok"] is False and "full title" in r["error"])
r = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 9})
check("a season that doesn't exist lists the ones that do", r["ok"] is False and "S1" in r["error"] and "S2" in r["error"])
check("a bad quality / audio / policy is an error", all(tools.tool_bulk_plan({"title": "Spirit Hunters", k: v})["ok"] is False
                                                       for k, v in (("quality", "999"), ("audio", "klingon"), ("on_missing", "x"))))
check("nothing asked yet queued anything", rows() == [])
check("no title asks", tools.tool_bulk_plan({}).get("needs_clarification"))

# ---- 3. run it: ONE group
before = len(AST.log)
run = tools.tool_bulk_run({"plan_id": plan["plan_id"]})
check("bulk_run queues the two exact episodes", run["ok"] and run["queued"] == [1, 2] and run["held_for_a_decision"] == [3])
check("as ONE group", len({r["group_id"] for r in rows()}) == 1 and rows()[0]["group_id"] == run["group"] and len(store.list_groups()) == 1)
check("the group is titled for the show, season and request", list(store.list_groups().values())[0]["title"] == "Spirit Hunters S02 (dubbed 1080p)")
r1 = next(r for r in rows() if r["options"]["episode"] == 1)
check("each row carries show / season / episode (the engine's file name and folder)",
      r1["options"]["show"] == "Spirit Hunters" and r1["options"]["season"] == 2 and r1["engine"] == "stremio" and r1["title"] == "Spirit Hunters S02E01")
check("the source is one canonical magnet", links.parse_magnet(r1["source_url"]) and r1["source_url"].startswith("magnet:?xt=urn:btih:"))
r2 = next(r for r in rows() if r["options"]["episode"] == 2)
check("E2 carries its next release as a fallback", len(r2["source_ref"]["fallbacks"]) == 1 and links.parse_magnet(r2["source_ref"]["fallbacks"][0]))
check("E1 has no fallback (only one dubbed 1080p exists), and none was invented", not (r1.get("source_ref") or {}).get("fallbacks"))
_dl_add = next(x for x in __import__("jarvis.actions.download_hub", fromlist=["x"]).TOOL_SCHEMAS if x["name"] == "dl_add")
check("no engine-specific argument was added to dl_add (the N4 rule)",
      not ({"show", "season", "episode", "fallbacks", "infohash", "file_idx", "stream"} & set(_dl_add["parameters"]["properties"])))

# ---- 4. a restart / a second run duplicates nothing
again = tools.tool_bulk_run({"plan_id": plan["plan_id"]})
check("running the plan again queues nothing new", again["ok"] and again["queued"] == [] and again["already_queued"] == [1, 2] and len(rows()) == 2)
store.transition(r1["id"], "downloading", expect="queued")
store.transition(r1["id"], "done", expect="downloading", dest_file=str(LIB / "Stremio" / "e1.mkv"))
again = tools.tool_bulk_run({"plan_id": plan["plan_id"]})
check("an episode already downloaded is skipped, not fetched twice", again["already_downloaded"] == [1] and again["already_queued"] == [2] and len(rows()) == 2)
# a run that died after the first row: only the missing one is added
fresh = tools.tool_bulk_plan({"title": "Spirit Hunters", "quality": "1080", "audio": "dub", "episodes": [1, 2, 3]})
check("a second plan for the same request joins the SAME group", fresh["group"] == plan["group"])

# ---- 5. the owner picks what to do about E3
p3 = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 2, "quality": "1080p", "audio": "dub", "episodes": [3],
                           "on_missing": "other_quality"})
check("with on_missing=other_quality E3 gets the dub at 720p, marked as a fallback with a note",
      p3["episodes"][0]["status"] == "fallback" and p3["episodes"][0]["pick"]["label"] == "720p dub" and "720p dub" in p3["episodes"][0]["note"])
run3 = tools.tool_bulk_run({"plan_id": p3["plan_id"]})
check("it joins the same group as a third row (no second group)", run3["queued"] == [3] and run3["group"] == run["group"]
      and len(rows()) == 3 and len(store.list_groups()) == 1)
p3b = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 2, "quality": "1080p", "audio": "dub", "episodes": [3], "on_missing": "skip"})
check("on_missing=skip leaves it out for good, and a plan with nothing to queue says so",
      p3b["episodes"][0]["status"] == "none" and tools.tool_bulk_run({"plan_id": p3b["plan_id"]})["ok"] is False)
p3c = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 2, "quality": "1080p", "audio": "dub", "episodes": [3], "on_missing": "other_audio"})
check("on_missing=other_audio keeps the resolution", p3c["episodes"][0]["pick"]["label"].startswith("1080p"))

# ---- 6. other seasons, other id schemes
s1 = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 1, "quality": "1080p", "audio": "dub"})
check("season 1 has no dubbed 1080p anywhere: every episode is pending, nothing is queued", s1["totals"]["pending"] == 3 and s1["totals"]["to_queue"] == 0
      and not s1["season_assumed"])
s1b = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 1, "quality": "720p", "audio": "sub"})
check("asking for 720p sub finds all three", s1b["totals"]["exact"] == 3)
anime = tools.tool_bulk_plan({"title": "Moon Pilot", "audio": "dub", "quality": "1080"})
check("an anime catalogued by Kitsu id plans through its own ids and type (only the addon that handles kitsu: ids is asked)",
      anime["ok"] and anime["totals"]["exact"] == 2 and {e["pick"]["addon"] for e in anime["episodes"]} == {"Fake Streams"})
check("...and its rows would be saved as Stremio/Moon Pilot/Season 01", anime["destination"].endswith("Stremio/Moon Pilot/Season 01"))
check("...and its two episodes are the whole 'season' (one season, so it isn't an assumption)", anime["season_assumed"] is False)

# ---- 7. caps and the disk check
old_cap = bulk.MAX_RUN_ITEMS
bulk.MAX_RUN_ITEMS = 1
cap_plan = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 1, "quality": "720p", "audio": "sub"})
r = tools.tool_bulk_run({"plan_id": cap_plan["plan_id"]})
check("a run over the cap is refused with the way out", r["ok"] is False and "override_caps" in r["error"] and len(rows()) == 3)
r = tools.tool_bulk_run({"plan_id": cap_plan["plan_id"], "override_caps": True, "start_paused": True})
check("override_caps lets it through, and start_paused queues them paused",
      r["ok"] and len(r["queued"]) == 3 and all(x["state"] == "paused" for x in rows() if x["options"]["season"] == 1))
bulk.MAX_RUN_ITEMS = old_cap
real_free = bulk._free_bytes
bulk._free_bytes = lambda path: 1024
disk_plan = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 2, "episodes": [1], "quality": "720p", "audio": "dub"})
r = tools.tool_bulk_run({"plan_id": disk_plan["plan_id"]})
bulk._free_bytes = real_free
check("not enough disk: refused before anything is queued", r["ok"] is False and "free space" in r["error"] and len(rows()) == 6)

# ---- 8. plan files are data, not trust
check("a plan id that isn't one is refused", tools.tool_bulk_run({"plan_id": "../../etc/passwd"})["ok"] is False)
check("an unknown plan id says to plan again", "run bulk_plan" in tools.tool_bulk_run({"plan_id": "bp000000000000"})["error"])
old = Path(HOME) / ".jarvis" / "hub" / "bulk_plans" / (plan["plan_id"] + ".json")
data = json.loads(old.read_text())
data["created"] = time.time() - 3 * 86400
old.write_text(json.dumps(data))
check("a plan older than a day is refused (the addons' answers change)", "more than a day" in tools.tool_bulk_run({"plan_id": plan["plan_id"]})["error"])
tamper = tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 2, "episodes": [2], "quality": "1080p", "audio": "dub"})
tp = Path(HOME) / ".jarvis" / "hub" / "bulk_plans" / (tamper["plan_id"] + ".json")
data = json.loads(tp.read_text())
data["episodes"][0]["pick"]["magnet"] = "http://169.254.169.254/latest/meta-data"
data["episodes"][0]["fallbacks"] = []
n = len(rows())
tp.write_text(json.dumps(data))
r = tools.tool_bulk_run({"plan_id": tamper["plan_id"]})
check("a tampered plan can't queue a link that isn't a magnet (re-checked at run time)", len(rows()) == n and not r.get("queued"))
check("the nothing-new case for E2 is a duplicate or skip, never a crash", r.get("skipped") is not None or r.get("ok") is False)

# ---- 9. the REAL Stremio engine falls back per episode (fake server, fake torrents)
store2 = Store(Path(HOME) / "hub-engine.db")
store2.set_settings({"library_root": str(LIB / "e2e"), "stremio.stall_seconds": 10, "stremio.metadata_timeout": 10})
e2_plan = bulk.load_plan(tools.tool_bulk_plan({"title": "Spirit Hunters", "season": 2, "episodes": [2], "quality": "1080p", "audio": "dub"})["plan_id"])
pick = links.parse_magnet(e2_plan["episodes"][0]["pick"]["magnet"])["infohash"]
fb = links.parse_magnet(e2_plan["episodes"][0]["fallbacks"][0]["magnet"])["infohash"]
SST.reset()
SST.torrents.update({pick: {"dead": True}, fb: {"name": "Fallback", "files": [("Fallback/Spirit.Hunters.S02E02.mkv", 9000)]}})
res = bulk.run_plan(e2_plan["id"], store=store2)
item = store2.get_item(store2.list_items(limit=5)[0]["id"])
check("the plan queued the episode with its fallback into a fresh store", res["queued"] == [2] and item["source_ref"]["fallbacks"])
claimed = store2.claim_next({"default": 100}, 100)


def run_engine(row):
    settings = store2.get_settings()
    settings["stremio"] = dict(settings["stremio"], metadata_timeout=1)
    ctx = EngineContext(store2, row, settings, stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    return seng.ENGINE.run(row, ctx)


out = run_engine(claimed)
row = store2.get_item(claimed["id"])
check("the first release has no seeders: the engine switches to the next one and says so",
      out.kind == Outcome.FAILED and out.retryable and "next release" in out.error and row["source_ref"]["active"]["infohash"] == fb)
claimed_row = dict(row, state="downloading")
out = run_engine(claimed_row)
final = Path(out.dest_file) if getattr(out, "dest_file", None) else None
check("the retry downloads the fallback into <library>/Stremio/<Show>/Season 02/<Show> - S02E02.mkv",
      out.kind == Outcome.DONE and final and final.exists()
      and final.relative_to(LIB / "e2e").as_posix() == "Stremio/Spirit Hunters/Season 02/Spirit Hunters - S02E02.mkv")
check("the group's progress counts it (what the 'batch finished' notice reads)", store2.group_progress(item["group_id"])["total"] == 1)

# ---- 10. the declarations
check("both tools are declared in the stremio group", tools.TOOL_GROUP == "stremio" and set(tools.TOOLS) == {"bulk_plan", "bulk_run"}
      and {s["name"] for s in tools.TOOL_SCHEMAS} == set(tools.TOOLS))
check("bulk_run is confirm-gated out of band, bulk_plan is not", tools.TOOL_CONFIRM_REQUIRED == {"bulk_run"})
check("no tool takes a path or a URL", not any(k in json.dumps(tools.TOOL_SCHEMAS) for k in ('"url"', '"path"', '"dest"')))
bad = [(n, checklist_schema.validate_entry(n, e)) for n, e in tools.TEST_CHECKLIST.items() if checklist_schema.validate_entry(n, e)]
check("every checklist entry is well formed", not bad and set(tools.TEST_CHECKLIST) == set(tools.TOOLS))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
