"""Download Hub phase 8 item 17.3-4: the `present_download_status` tool (actions/download_status_card.py).

Covers: the tool loading through the real loader (one tool, in the `downloads` group so it is present in the
same turn as dl_add, a checklist entry, no confirm gate because it is read-only, a result spec that keeps the
card payload away from the model); the card line it prints (one `download_status` JSON field, no tab or newline,
only the fields the card draws, at most eight cards); the text result a channel without a web page still gets;
its refusals (no id, no such download, nothing asked); that it never writes to the hub; and that the card
reaches a reloaded conversation (thread_extras.py rebuilds a `downloadStatus` extra from the tool's own result).

Run: python tests/test_download_status_card.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-dl-card-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI))

from jarvis import hub_admin, thread_extras, tool_loader, tool_registry, wire  # noqa: E402
from jarvis.actions import download_status_card as card  # noqa: E402
from jarvis.hub import paths  # noqa: E402,F401
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
PUBLIC = "http://93.184.216.34"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def run_tool(**kw):
    """Run the tool with a default Store under HOME; returns (parsed result, the card lines printed)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = card.TOOLS["present_download_status"](**kw)
    lines = [l for l in err.getvalue().splitlines() if wire.MEDIA_PREFIX in l]
    return json.loads(out), lines


def test_loading():
    records = tool_loader.discover_actions(logger=lambda *a, **k: None)
    mine = [r for r in records if r.file.startswith("download_status_card")]
    check("the loader finds the file and it is valid", len(mine) == 1 and mine[0].valid and not mine[0].error)
    rec = mine[0]
    check("it defines exactly present_download_status", sorted(rec.tools) == ["present_download_status"])
    check("it is in the `downloads` group (routing is by group, and it must be there in the turn dl_add runs)", rec.group == "downloads"
          and "present_download_status" in tool_registry.TOOL_GROUPS.get("downloads", []))
    check("it is read-only: no confirm gate", "present_download_status" not in rec.confirm_required)
    check("it has a checklist entry", "present_download_status" in rec.checklist)
    check("the card payload is dropped from what the model reads", "present_download_status" in rec.result_specs)
    check("no other file defines the same tool", all("present_download_status" not in r.tools for r in records if r is not rec and r.valid))
    check("the schema takes ids, group, query and requires none", set(rec.schemas[0]["parameters"]["properties"]) == {"ids", "group", "query"}
          and rec.schemas[0]["parameters"]["required"] == [])
    check("the description tells the model to call it once", "once" in rec.schemas[0]["description"].lower() and "never call it repeatedly" in rec.schemas[0]["description"].lower())


def test_pure():
    row = {"id": "h1", "title": "Big file", "state": "downloading", "percent": 41.6, "speed_bps": 1048576}
    check("the one-line status of a running download", card.summary_line(row) == "Big file: downloading, 42%, 1.0 MB/s")
    check("a finished one doesn't repeat 100%", card.summary_line({"title": "x", "state": "done", "percent": 100}) == "x: done")
    check("a failure carries its reason", "HTTP 404" in card.summary_line({"title": "x", "state": "failed", "error": "HTTP 404"}))
    check("a batch reads as counts", card.summary_line({"card": "group", "title": "Pack", "counts": {"done": 2, "total": 5, "failed": 1, "running": 1}, "percent": 50})
          == "Pack: 2 of 5 done, 1 failed, 1 running (50%)")
    check("a row without a title still has words", card.summary_line({"state": "queued"}) == "download: queued")
    payload = card.card_line_payload([dict(row, secret_path="C:/x", source_url="http://x", has_thumb=True, owner="hub")] * 12)
    check("at most eight cards go to the page", len(payload["cards"]) == 8 and len(payload["ids"]) == 8)
    check("only the fields the card draws go, no paths or links", all("secret_path" not in c and "source_url" not in c for c in payload["cards"]) and payload["cards"][0]["has_thumb"] is True)
    check("groups and items are told apart", card.card_line_payload([{"id": "g", "card": "group"}, {"id": "i", "card": "item"}])["groups"] == ["g"])


def test_tool():
    st = Store()
    a = st.add_item(engine="http", source_url=PUBLIC + "/a.bin", title="a.bin")[0]
    st.update_item(a["id"], {"state": "downloading", "percent": 25, "bytes_done": 25, "bytes_total": 100, "speed_bps": 2048})
    before = st.counts()
    res, lines = run_tool(ids=[a["id"]])
    check("ok, one card shown, a status sentence for a channel with no web page", res["ok"] and res["shown"] == 1 and res["status"][0].startswith("a.bin: downloading"))
    check("exactly one card line was printed", len(lines) == 1)
    fields = lines[0].split("\t")
    check("the line is a JARVIS_MEDIA card", wire.MEDIA_PREFIX in fields[0])
    check("its kind is download_status and it has ONE field", fields[1] == "download_status" and len(fields) == 3)
    body = json.loads(fields[2])
    check("the field is JSON naming the id and the last known status", body["ids"] == [a["id"]] and body["cards"][0]["state"] == "downloading" and body["cards"][0]["percent"] == 25)
    check("the line has no tab or newline inside the JSON", "\t" not in fields[2] and "\n" not in fields[2])
    check("the tool's own result carries the payload for the reload (the model's view drops it)", res["card"]["ids"] == [a["id"]])
    check("it changed nothing in the hub", st.counts() == before)
    res2, lines2 = run_tool(query="a.b")
    check("a title is enough when there is no id", res2["ok"] and len(lines2) == 1)
    for kw, why in (({}, "nothing asked"), ({"ids": ["nope"]}, "no such download"), ({"ids": []}, "empty ids"), ({"query": "zzzzzz"}, "no match")):
        r, ln = run_tool(**kw)
        check("%s: ok false, a problem in words, and no card is drawn" % why, r["ok"] is False and r.get("problem") and ln == [])
    r3, _ = run_tool(ids="not-a-list-but-a-string")
    check("a string instead of a list is tolerated, not a crash", r3["ok"] is False)


def test_reload():
    st = Store()
    a = st.add_item(engine="http", source_url=PUBLIC + "/r.bin", title="r.bin")[0]
    res, _ = run_tool(ids=[a["id"]])
    runs = [{"name": "present_download_status", "arguments": {"ids": [a["id"]]}, "result": res}]
    extras = thread_extras.extras_from_runs(runs) if hasattr(thread_extras, "extras_from_runs") else None
    check("the card is rebuilt from the tool's result when a conversation is loaded again",
          extras is not None and any(e.get("type") == "downloadStatus" and e["data"]["ids"] == [a["id"]] for e in extras))
    check("`downloadStatus` is a tool-derived type, so it is rebuilt rather than stored twice", "downloadStatus" in thread_extras.TOOL_DERIVED_TYPES)
    bad = [{"name": "present_download_status", "arguments": {}, "result": {"ok": False, "problem": "x"}}]
    check("a refused call leaves no card behind", not any(e.get("type") == "downloadStatus" for e in (thread_extras.extras_from_runs(bad) or [])))


def test_front_end_plumbing():
    app = (ROOT / "web" / "public" / "app.js").read_text(encoding="utf-8")
    check("app.js draws the card on a download_status media line and from a saved extra", 'parts[1] === "download_status"' in app and 'case "downloadStatus"' in app)
    check("the media kind is generic: server.js needs no whitelist entry", "MEDIA_KIND_RE" in (ROOT / "web" / "server.js").read_text(encoding="utf-8"))
    check("the module doesn't import the hub at load time (tool files must stay light)", "\nfrom " not in (CLI / "jarvis" / "actions" / "download_status_card.py").read_text(encoding="utf-8").split('"""', 2)[2].split("def ")[0])


if __name__ == "__main__":
    for fn in (test_loading, test_pure, test_tool, test_reload, test_front_end_plumbing):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
