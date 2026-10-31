"""Download Hub, link-resolver plan phase 0/1: dl_add_hoster, hoster_resolve and hoster_doctor.

Covers: the real loader has them (downloads group, keywords, checklist entries that pass the schema, no pack instruction
of their own); a chat guest is refused by every handler and not offered them, and nothing is contacted for a guest;
dl_add_hoster forces the hosters engine (a plain link is refused with the reason) and queues a real row; hoster_resolve
describes the resolver, probes the host and NEVER prints the file address; hoster_doctor runs the canary links, records
what it saw, and reports what it could not check.

Run: python tests/test_hub_hosters_tools.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-htools-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_hosters as fake  # noqa: E402
from jarvis import checklist_schema, tool_registry, tools  # noqa: E402
from jarvis.actions import download_add_http as dah  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.actions import hosters as H  # noqa: E402
from jarvis.hub import hosters  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
SERVER, BASE, STATE = fake.start()
NAMES = ("dl_add_hoster", "hoster_resolve", "hoster_doctor")


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def store():
    return Store()


def configure(**extra):
    s = {"allow_private_hosts": ["127.0.0.1"], "hosters.extra_hosts": {"buzzheavier": ["127.0.0.1"]}}
    s.update(extra)
    store().set_settings(s)


def rows():
    return store().list_items(owner="hub", limit=200)


configure()

# ---------------------------------------------------------------- registration
check("the real loader has all three tools", all(n in tools.TOOLS for n in NAMES))
check("they are in the downloads group", all(n in tool_registry.TOOL_GROUPS["downloads"] for n in NAMES))
check("each has a checklist entry that passes the schema",
      all(n in H.TEST_CHECKLIST and checklist_schema.validate_entry(n, H.TEST_CHECKLIST[n]) == [] for n in NAMES))
check("each has keywords", all(H.TOOL_KEYWORDS.get(n) for n in NAMES))
taken = set(dh.TOOL_KEYWORDS["dl_add"]) | set(dah.TOOL_KEYWORDS["dl_add_http"]) | {"download video", "download youtube", "download this video"}
mine = [p for n in NAMES for p in H.TOOL_KEYWORDS[n]]
check("none of dl_add's, dl_add_http's or another hoster tool's phrases is reused", not (set(mine) & taken) and len(mine) == len(set(mine)))
check("it declares no pack instruction (the group has one, in download_hub.py)", not hasattr(H, "TOOL_PACK_INSTRUCTION"))
check("the two tools that contact a host are owner-only by name; dl_add_hoster by its dl_add_ prefix",
      {"hoster_resolve", "hoster_doctor"} <= tools.OWNER_ONLY_TOOLS)
check("dl_add_hoster is not confirm-gated by name (it asks its questions itself)", "dl_add_hoster" not in dh.TOOL_CONFIRM_REQUIRED)
schemas = {s["name"]: s for s in H.TOOL_SCHEMAS}
check("schemas exist for exactly these tools", set(schemas) == set(NAMES) and all(schemas[n]["parameters"]["type"] == "object" for n in NAMES))

# ---------------------------------------------------------------- guest
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
STATE.reset()
n = len(rows())
for name, args in (("dl_add_hoster", {"url": BASE + "/goodfile"}), ("hoster_resolve", {"url": BASE + "/goodfile"}),
                   ("hoster_doctor", {})):
    r = H.TOOLS[name](args)
    check("a chat guest is refused by %s" % name, r.get("ok") is False)
check("and nothing was queued or contacted for them", len(rows()) == n and STATE.reqs == [])
hidden = tools.hidden_from_sender()
check("a guest is not even offered them", all(x in hidden for x in NAMES))
os.environ.pop("JARVIS_CHANNEL_SENDER")

# ---------------------------------------------------------------- dl_add_hoster
STATE.reset()
r = H.tool_dl_add_hoster({})
check("no link asks which one", r.get("needs_clarification") is True)
r = H.tool_dl_add_hoster({"url": "https://example.com/file.zip", "dry_run": True})
d = r["decisions"][0]
check("a plain link is refused with the reason (the hosters engine can't take it); nothing queued",
      r["ok"] and r["dry_run"] and d["ok"] is False and "hosters engine can't take" in d["problem"] and rows() == [])
r = H.tool_dl_add_hoster({"url": BASE + "/goodfile", "dry_run": True})
d = r["decisions"][0]
check("a hoster link dry-runs to the hosters engine with a readable reason", d["ok"] and d["engine"] == "hosters" and d["reason"] == "you chose the hosters engine" and d["problem"] is None)
check("a dry run contacts nothing", STATE.reqs == [])
r = H.tool_dl_add_hoster({"url": BASE + "/goodfile", "start_paused": True, "group": "my files"})
made = rows()
check("a real add queues one paused hosters row that keeps the PAGE link",
      r.get("ok") and len(made) == 1 and made[0]["engine"] == "hosters" and made[0]["state"] == "paused" and made[0]["source_url"] == BASE + "/goodfile")
check("queueing contacts nothing either (resolving happens when it runs)", STATE.reqs == [])

# ---------------------------------------------------------------- hoster_resolve
r = H.tool_hoster_resolve({})
by_id = {x["id"]: x for x in r["supported"]}
check("with no link it lists the supported hosts with their honesty fields", r["ok"] and {"buzzheavier", "gdrive", "mediafire"} <= set(by_id)
      and all(by_id[i]["confidence"] and by_id[i]["method"] and by_id[i]["last_check"] is None for i in ("buzzheavier", "gdrive", "mediafire")))
check("and it reports a host the owner added", "127.0.0.1" in by_id["buzzheavier"]["hosts"])

STATE.reset()
r = H.tool_hoster_resolve({"url": BASE + "/goodfile"})
e = r["links"][0]
dump = json.dumps(r)
check("it describes the resolver and probes the host", e["handled"] and e["resolver"] == "buzzheavier" and e["probe"]["status"] == "ok" and e["probe"]["kind"] == "file")
check("it reports the file host and the header NAMES it would send", e["probe"]["file_host"] == "127.0.0.1" and e["probe"]["sends_headers"] == ["Referer", "User-Agent"])
check("it never prints the file address, its token, or a header value", "/fdl/" not in dump and "?t=" not in dump and "Mozilla" not in dump)
check("and it downloaded no file", not any(p.startswith("/fdl/") for p, _ in STATE.reqs))

STATE.reset()
r = H.tool_hoster_resolve({"url": BASE + "/goodfile", "probe": False})
check("probe=false describes without contacting anyone", r["links"][0]["handled"] and "probe" not in r["links"][0] and STATE.reqs == [])
r = H.tool_hoster_resolve({"url": "https://example.com/a.zip"})
check("a plain link is 'not handled' and says which engine would take it", r["links"][0]["handled"] is False and r["links"][0]["would_use"] == "http")
for fid, status, extra in (("gone", "dead", None), ("busy", "quota", ("retry_after_seconds", 120)),
                           ("nohdr", "page_not_understood", None), ("err500", "temporary_problem", None)):
    p = H.tool_hoster_resolve({"url": BASE + "/" + fid})["links"][0]["probe"]
    check("a %s answer is reported as %s%s" % (fid, status, " with its wait" if extra else ""),
          p["status"] == status and (not extra or p.get(extra[0]) == extra[1]))
r = H.tool_hoster_resolve({"urls": [BASE + "/goodfile"] * 8, "probe": False})
check("at most five links are looked at, and it says so", len(r["links"]) == 5 and r.get("note") == "Only the first 5 links were looked at.")

# ---------------------------------------------------------------- hoster_doctor
STATE.reset()
r = H.tool_hoster_doctor({})
rows_by = {x["resolver"]: x for x in r["resolvers"]}
check("with no canary links every resolver says so and nothing is contacted",
      r["ok"] and all(x["status"] == "no_canary" for x in rows_by.values()) and "hosters.canary" in rows_by["mediafire"]["note"] and STATE.reqs == [])
check("it reports resolvers that failed to load (none here)", r["load_errors"] == {})
check("an unknown host is an error naming the known ones", "known:" in H.tool_hoster_doctor({"host": "nope"})["error"])

configure(**{"hosters.canary": {"buzzheavier": BASE + "/goodfile"}})
r = H.tool_hoster_doctor({"host": "buzzheavier"})
row = r["resolvers"][0]
check("the host filter returns one row", len(r["resolvers"]) == 1 and row["resolver"] == "buzzheavier")
check("a working canary is reported working, with the time it was checked", row["status"] == "ok" and row["working"] is True and row["at"])
health = hosters.health(store().get_settings(), "buzzheavier")
check("what it saw is recorded as the resolver's health (from observation, not a claim)", health and health["ok"] is True and health["at"] == row["at"])
check("hoster_resolve now shows that last check", H.tool_hoster_resolve({"url": BASE + "/goodfile", "probe": False})["links"][0]["last_check"]["ok"] is True)

configure(**{"hosters.canary": {"buzzheavier": BASE + "/gone"}})
row = H.tool_hoster_doctor({"host": "buzzheavier"})["resolvers"][0]
check("a dead canary says the CANARY looks dead (pick another), and records a failure",
      row["status"] == "dead" and row["working"] is False and "canary link itself looks dead" in row["note"]
      and hosters.health(store().get_settings(), "buzzheavier")["ok"] is False)
configure(**{"hosters.canary": {"buzzheavier": BASE + "/busy"}})
row = H.tool_hoster_doctor({"host": "buzzheavier"})["resolvers"][0]
check("a quota means the flow reached its last step, so it counts as working", row["status"] == "quota" and row["working"] is True)
configure(**{"hosters.canary": {"buzzheavier": BASE + "/nohdr"}})
row = H.tool_hoster_doctor({"host": "buzzheavier"})["resolvers"][0]
check("a page nobody expects is a failure (the site changed)", row["status"] == "page_not_understood" and row["working"] is False)
configure(**{"hosters.canary": {"buzzheavier": BASE + "/err500"}})
row = H.tool_hoster_doctor({"host": "buzzheavier"})["resolvers"][0]
check("a temporary problem is 'could not tell' (None), not a failure", row["status"] == "temporary_problem" and row["working"] is None)
configure(**{"hosters.enabled": {"buzzheavier": False}})
row = H.tool_hoster_doctor({"host": "buzzheavier"})["resolvers"][0]
check("a switched-off resolver is reported disabled and not contacted", row["status"] == "disabled")

# ---------------------------------------------------------------- a tool never raises
real = hosters.resolvers
hosters.resolvers = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    r = H.tool_hoster_doctor({})
finally:
    hosters.resolvers = real
check("a failure inside a tool is an error answer, not an exception", r.get("ok") is False and "boom" in r.get("error", ""))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
