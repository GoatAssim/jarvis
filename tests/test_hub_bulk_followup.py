"""The "yes" after a season plan (jarvis log 2026-10-25): the chat only carries the WORDS of earlier turns, so the plan_id
from bulk_plan was gone when the owner said yes; the agent searched again, and the same show came back under several ids
("which one?").  Proves: bulk_run with no plan_id names the newest fresh plan and queues nothing; the plan message tells
the model to write the id in its reply; media_find ranks the same way every time and one show listed under an addon's own
ids next to its IMDb id is ONE title.  No network.

Run: python tests/test_hub_bulk_followup.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-bulk-followup-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.actions import bulk_download as tools  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import bulk, discovery, ranking  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


WANT = ranking.normalize_want(None, None, None, None)[0]


def fake_plan(plan_id, created, title="Rick and Morty", season=1, episodes=9):
    return {"id": plan_id, "created": created, "media": {"id": "tt2861424", "type": "series", "title": title, "year": 2013, "images": {}},
            "season": season, "seasons_available": [{"season": season, "episodes": episodes, "released": episodes}],
            "season_assumed": False, "want": WANT, "policy": "ask", "group_id": "gb_test", "group_title": "%s S%02d" % (title, season),
            "destination": "C:/x/%s" % title,
            "episodes": [{"episode": n, "title": "", "status": "exact", "pick": {"label": "1080p", "size_gb": 0.3, "seeders": 5, "addon": "A", "magnet": ""}}
                         for n in range(1, episodes + 1)],
            "totals": {"episodes": episodes, "exact": episodes, "fallback": 0, "pending": 0, "none": 0, "unreleased": 0, "unanswered": 0,
                       "to_queue": episodes, "size_gb": 2.7, "unknown_size": 0}, "notes": []}


# ---- bulk_run with no plan_id
empty = tools.tool_bulk_run({})
check("no plan saved and no plan_id: the old question, 'Which plan?'", empty.get("needs_clarification") and "Which plan?" in empty["message"])

bulk.save_plan(fake_plan("bp0000000000aa", time.time() - 3600, title="Old Show"))
bulk.save_plan(fake_plan("bp0000000000bb", time.time() - 120))
os.utime(bulk.plans_dir() / "bp0000000000aa.json", (time.time() - 3600, time.time() - 3600))
named = tools.tool_bulk_run({})
check("no plan_id: it names the NEWEST fresh plan and its id", named.get("newest_plan_id") == "bp0000000000bb"
      and "plan_id=bp0000000000bb" in named["message"] and "Rick and Morty" in named["message"])
check("... and says nothing is queued (the call that carries the id is the one that asks the owner)",
      named["ok"] is False and "Nothing is queued" in named["message"] and not Store().list_items(limit=50))

stale = bulk.plans_dir() / "bp0000000000cc.json"
bulk.save_plan(fake_plan("bp0000000000cc", time.time() - 3 * 24 * 3600, title="Stale Show"))
os.utime(stale, (time.time() + 60, time.time() + 60))          # newest file, but older than a day inside
check("a plan older than a day is never offered", bulk.newest_plan()["id"] == "bp0000000000bb")
check("bulk_run with a stale plan_id still refuses as before", tools.tool_bulk_run({"plan_id": "bp0000000000cc"}).get("ok") is False)

# ---- the message the model reads
msg = bulk.plan_message(fake_plan("bp0000000000dd", time.time()))
check("the plan message carries the id and asks the model to write it in its reply", "plan_id=bp0000000000dd" in msg and "in your reply" in msg)

# ---- media_find: one show, several ids
def cand(id_, name="Rick and Morty", year=2013, addon="x", type_="series", score=1.0):
    from jarvis.hub.stremio import addons as A
    return {"id": id_, "type": type_, "name": name, "year": year, "id_kind": A.id_kind(id_), "addon": addon, "score": score}


dupes = [cand("mf1901739", addon="MediaFusion"), cand("mf1901738", addon="MediaFusion"), cand("tt2861424", addon="Cinemeta"),
         cand("tt28466465", name="Rick and Morty: The Anime", year=2024, score=0.6), cand("mf1934563", addon="MediaFusion")]
ranked = sorted(dupes, key=lambda c: (-c["score"], -(c["year"] or 0), discovery._KIND_RANK.get(c.get("id_kind"), 2)))
collapsed = discovery._collapse_same_title(ranked)
check("sorted by id kind first, the IMDb id is the one that stays", [c["id"] for c in collapsed if c["name"] == "Rick and Morty"] == ["tt2861424"])
check("so a plain name is a clear winner (no 'which one?')", discovery.is_clear_winner(collapsed) and collapsed[0]["id"] == "tt2861424")
check("addon ids are kept when no IMDb / Kitsu twin exists", len(discovery._collapse_same_title([cand("mf1", name="Obscure"), cand("mf2", name="Obscure")])) == 2)
check("two different shows with one name (other year) are NOT merged", len(discovery._collapse_same_title(
    [cand("tt1", name="Fargo", year=1996, type_="movie"), cand("tt2", name="Fargo", year=2014)])) == 2)

# ---- Dismiss on a cancelled / failed hub download (2026-10-25: a cancelled one offered only Retry)
from jarvis import hub_admin  # noqa: E402
st = Store()
mk = lambda n, state: st.add_item(engine="http", source_url="http://93.184.216.34/%s.bin" % n, title=n)[0]  # noqa: E731
rows_by = {}
for name, state in (("canc", "cancelled"), ("fail", "failed"), ("done", "done"), ("run", "downloading"), ("wait", "queued")):
    item = mk(name, state)
    st.update_item(item["id"], {"state": state})
    rows_by[name] = item["id"]
r = hub_admin.admin("dismiss", {"id": rows_by["canc"]}, store=st)
check("a cancelled hub download can be hidden", r["ok"] and st.get_item(rows_by["canc"])["dismissed_at"] is not None)
r = hub_admin.admin("dismiss", {"id": rows_by["fail"]}, store=st)
check("so can a failed one", r["ok"] and st.get_item(rows_by["fail"])["dismissed_at"] is not None)
for name in ("done", "run", "wait"):
    r = hub_admin.admin("dismiss", {"id": rows_by[name]}, store=st)
    check("a %s hub download is still refused (History / dl_cancel own those)" % name, r["ok"] is False and st.get_item(rows_by[name])["dismissed_at"] is None)
check("a hidden row is gone from the list but not from the database", rows_by["canc"] not in [x["id"] for x in st.list_items(limit=50)]
      and rows_by["canc"] in [x["id"] for x in st.list_items(include_dismissed=True, limit=50)])
st.transition(rows_by["canc"], "queued", expect="cancelled")
check("Retry on a hidden row brings it back", st.get_item(rows_by["canc"])["dismissed_at"] is None)
c2 = mk("canc2", "cancelled")
st.update_item(c2["id"], {"state": "cancelled"})
f2 = mk("fail2", "failed")
st.update_item(f2["id"], {"state": "failed"})
allr = hub_admin.admin("dismiss", {"finished": True}, store=st)
check("Clear finished takes the cancelled hub download and leaves the failed one", c2["id"] in allr["dismissed"] and f2["id"] not in allr["dismissed"])

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
