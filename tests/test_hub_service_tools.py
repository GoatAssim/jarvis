"""Download Hub, phases 1-2: the service layer, the tool file, hub_client, the demo and hub_doctor.

Covers: the tool file loading through the real loader (11 tools, group `downloads`, the
Menu panel and the daemon it declares, a checklist entry per tool, confirm gating by name,
routing that leaves `ytdl_download` its phrases, `_hub_daemon.py` not treated as a tool, the
panel's files existing and every element id tool.js reaches for being in tool.html, the
package-data line); service.add_items (queueing, groups, dedupe, the SSRF refusal, magnet /
YouTube answers, `dest` as a sub-folder only, caps); listing (state words, masking, which
buttons a row offers per state and per provider); settings; open_item (only inside the library
root or a provider's open_roots); the tool handlers (including the bulk-add confirmation);
hub_client (idempotent begin, coalescing, never raises, spool and drain, namespacing, rate
bucket); the demo provider (a pure function of time, going stale, stop / clear); hub_doctor.

Run: python tests/test_hub_service_tools.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-svc-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI))

from jarvis import hub_client, tool_loader, tool_registry, tool_router, ui_bridge  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import demo, doctor, engines, paths, service, states  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]
PUBLIC = "http://93.184.216.34"          # a literal public address: no DNS in these tests


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


class Clock:
    def __init__(self, t=3_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def fresh_store(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / f"hub-{_n[0]}.db", clock=clock or time.time)


TOOL_NAMES = ["dl_add", "dl_list", "dl_status", "dl_pause", "dl_resume", "dl_cancel", "dl_retry",
              "dl_schedule", "dl_open", "dl_settings", "dl_prefs", "hub_doctor", "hub_demo_provider"]


# ---------------------------------------------------------------- the tool file, through the real loader

def test_tool_file():
    records = tool_loader.discover_actions(logger=lambda *a, **k: None)
    mine = [r for r in records if r.file.startswith("download_hub")]
    check("the loader finds download_hub.py", len(mine) == 1)
    rec = mine[0]
    check("it is valid (no load error)", rec.valid and not rec.error)
    check("it defines exactly the 13 tools", sorted(rec.tools) == sorted(TOOL_NAMES) and sorted(s["name"] for s in rec.schemas) == sorted(TOOL_NAMES))
    check("in the new `downloads` group", rec.group == "downloads")
    check("no tool name collides with another file's", all(set(rec.tools).isdisjoint(set(o.tools)) for o in records if o is not rec and o.valid))
    check("cancel, settings and the demo are confirm-gated by name",
          rec.confirm_required == {"dl_cancel", "dl_settings", "hub_demo_provider"})
    check("every tool has a checklist entry", sorted(rec.checklist) == sorted(TOOL_NAMES))
    check("and the group has a label for the checklist", rec.checklist_group and list(rec.checklist_group.values())[0]["label"])
    check("a result spec exists for the two big list results", {"dl_list", "dl_status"} <= set(rec.result_specs))

    check("one Menu panel is declared and validated", [u["id"] for u in rec.ui] == ["downloads_panel"] and rec.ui[0]["mode"] == "menu")
    ui_dir = Path(rec.ui[0]["_dir"])
    check("the panel folder is hub_ui next to the tool file", ui_dir == CLI / "jarvis" / "actions" / "hub_ui")
    html = (ui_dir / "tool.html").read_text(encoding="utf-8")
    js = (ui_dir / "tool.js").read_text(encoding="utf-8")
    css = (ui_dir / "tool.css").read_text(encoding="utf-8")
    check("all three panel files exist and have content", html.strip() and js.strip() and css.strip())
    wanted = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js))
    have = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    check("every element id tool.js looks up is in tool.html", wanted and wanted <= have)
    runs = set(re.findall(r'(?:runTool|callTool)\("([a-z_]+)"', js)) | set(re.findall(r'tool: "([a-z_]+)"', js))
    check("the panel only runs tools of this file", runs and runs <= set(TOOL_NAMES))
    check("the panel never builds markup from data", "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js and "eval(" not in js)
    check("the package declares the panel files (a real install copies them)", "hub_ui/*.js" in (CLI / "pyproject.toml").read_text(encoding="utf-8"))

    check("one daemon is declared", [d["id"] for d in rec.daemons] == ["hub"] or [d.get("did") for d in rec.daemons] == ["download_hub-hub"])
    daemon = rec.daemons[0]
    check("it autostarts and restarts always", daemon["autostart"] is True and daemon["restart"] == "always")
    check("its command runs the runner script that sits next to the tool file",
          daemon["argv"][-1].replace("\\", "/").endswith("actions/_hub_daemon.py"))
    check("the runner exists and is skipped by the loader", (CLI / "jarvis" / "actions" / "_hub_daemon.py").exists()
          and not any(r.file.startswith("_hub_daemon") and r.valid for r in records))

    check("the tools are in the registry's `downloads` group", sorted(tool_registry.TOOL_GROUPS.get("downloads", [])) == sorted(TOOL_NAMES))
    route = tool_router.route
    check("'what's downloading' routes to the downloads group", "downloads" in route("what's downloading right now?").groups)
    check("'download this file <url>' routes there", "downloads" in route("download this file https://example.com/a.zip").groups)
    check("'download this video' stays with the video tool", "downloads" not in route("download this video https://youtube.com/watch?v=x").groups)
    check("'download youtube' stays with the video tool", "downloads" not in route("download youtube video please").groups)
    check("'open my downloads folder' is not a hub request", "downloads" not in route("open my downloads folder").groups)
    check("'pause all downloads' routes to the hub", "downloads" in route("pause all downloads").groups)

    schema = {s["name"]: s for s in rec.schemas}
    check("dl_add tells the model where its limits are", "ytdl_download" in schema["dl_add"]["description"] and "dry_run" in schema["dl_add"]["parameters"]["properties"])
    check("dl_open takes an id, never a path", list(schema["dl_open"]["parameters"]["properties"]) == ["id", "action", "target"] and "path" not in json.dumps(schema["dl_open"]))
    check("dl_add's dest says sub-folder, not a path", "sub-folder" in schema["dl_add"]["parameters"]["properties"]["dest"]["description"])


# ---------------------------------------------------------------- adding

def test_add_items():
    store = fresh_store()
    res = service.add_items([PUBLIC + "/a.zip"], store=store)
    item = res["added"][0]
    check("a link is queued on the HTTP engine", res["ok"] and item["engine"] == "http" and item["state"] == "queued" and item["title"] == "a.zip" and not item["duplicate"])
    check("with a note when the daemon isn't running", "daemon isn't running" in res.get("note", ""))
    check("and the library root is reported", res["library_root"].endswith(os.path.join("Downloads", "Jarvis")))
    again = service.add_items([PUBLIC + "/a.zip"], store=store)
    check("the same link again returns the same item, flagged duplicate", again["added"][0]["id"] == item["id"] and again["added"][0]["duplicate"])
    check("a different spelling of the same link is a duplicate too", service.add_items(["HTTP://93.184.216.34:80/a.zip#x"], store=store)["added"][0]["duplicate"])
    check("only one row exists", len(store.list_items(limit=100)) == 1)

    multi = service.add_items([PUBLIC + "/b.zip", PUBLIC + "/c.zip"], store=store)
    rows = [store.get_item(a["id"]) for a in multi["added"]]
    check("several links become one group", multi["group"] and rows[0]["group_id"] == rows[1]["group_id"] == multi["group"])
    check("the group has a default title", store.list_groups()[multi["group"]]["title"] == "Batch of 2")
    named = service.add_items([PUBLIC + "/d.zip"], group="Season 1", store=store)
    check("a named group is created even for one link", named["group"] and store.list_groups()[named["group"]]["title"] == "Season 1")

    bad = service.add_items(["http://192.168.1.1/admin"], store=store)
    check("a private address is refused with the reason", bad["ok"] is False and "private" in bad["error"] and bad["skipped"])
    mixed = service.add_items([PUBLIC + "/e.zip", "http://10.0.0.1/x"], store=store)
    check("one good and one bad: the good is queued, the bad listed", mixed["ok"] and mixed["count"] == 1 and len(mixed["skipped"]) == 1)
    magnet = service.add_items(["magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"], store=store)
    check("a magnet link says the stremio engine isn't installed", magnet["ok"] is False and "stremio" in magnet["error"])
    yt = service.add_items(["https://www.youtube.com/watch?v=abc"], store=store)
    check("a YouTube page is not queued as a file", yt["ok"] is False and "ytdlp" in yt["error"])
    check("nothing but the good links were queued", len(store.list_items(limit=100)) == 5)

    check("empty input is a clear error", service.add_items([], store=store)["error"].startswith("give me a link"))
    check("too many links in one call is refused", "limit is 50" in service.add_items([PUBLIC + "/%d" % i for i in range(51)], store=store)["error"])
    check("a string is accepted as one link", service.add_items(PUBLIC + "/f.zip", store=store)["ok"])
    check("an unknown engine is refused", service.add_items([PUBLIC + "/g.zip"], engine="nope", store=store)["ok"] is False)

    paused = service.add_items([PUBLIC + "/h.zip"], start_paused=True, store=store)
    check("start_paused queues it paused", paused["added"][0]["state"] == "paused")
    sub = service.add_items([PUBLIC + "/i.zip"], dest="Manuals", store=store)
    check("dest is stored as a sub-folder option", store.get_item(sub["added"][0]["id"])["options"] == {"subdir": "Manuals"})
    for bad_dest in ("../x", "/etc", "C:\\Windows", "a/b/c/d/e/f"):
        res = service.add_items([PUBLIC + "/j.zip"], dest=bad_dest, store=store)
        check("dest %r is refused as not a sub-folder" % bad_dest, res["ok"] is False and "sub-folder" in res["error"])
    same_link_other_dir = service.add_items([PUBLIC + "/i.zip"], dest="Other", store=store)
    check("the same link into a different folder is a separate download", not same_link_other_dir["added"][0]["duplicate"])
    host_only = service.add_items(["http://93.184.216.34/"], store=store)
    check("a link with no file name is titled by its host", host_only["added"][0]["title"] == "93.184.216.34")

    check("preview refuses loopback", service.preview("http://127.0.0.1/x", store=store)["ok"] is False)
    store.set_settings({"allow_private_hosts": ["127.0.0.1"]})
    check("...until the owner allow-lists it", service.preview("http://127.0.0.1/x", store=store)["ok"])
    count = len(store.list_items(limit=500))
    service.preview(PUBLIC + "/never-queued.zip", store=store)
    check("preview queues nothing", len(store.list_items(limit=500)) == count)


# ---------------------------------------------------------------- reading

def test_listing_status_settings():
    store = fresh_store()
    service.add_items([PUBLIC + "/a.zip?token=SECRET"], store=store)
    row = service.list_items(store=store)["items"][0]
    check("the list masks the query string of a source", row["source"] == "http://93.184.216.34/a.zip?…" and "SECRET" not in json.dumps(row))
    check("a queued hub row offers pause and cancel", row["capabilities"] == ["pause", "cancel"] and not row["read_only"])
    check("state filters accept a word", service.list_items(state="queued", store=store)["count"] == 1 and service.list_items(state="active", store=store)["count"] == 0)
    check("and several, and the group words", service.list_items(state="queued,paused", store=store)["count"] == 1 and service.list_items(state="waiting", store=store)["count"] == 1)
    check("an unknown state is an error naming the valid ones", "unknown state" in service.list_items(state="bogus", store=store)["error"])
    withsum = service.list_items(with_summary=True, store=store)["summary"]
    check("the summary has counts, daemon, library root and free disk",
          withsum["counts"]["queued"] == 1 and withsum["daemon"]["alive"] is False and withsum["library_root"] and "free_bytes" in withsum)

    caps = service.effective_capabilities
    hub = lambda s, **kw: dict({"owner": "hub", "engine": "http", "state": s, "dest_file": None}, **kw)  # noqa: E731
    check("capabilities per state: running / queued", caps(hub("downloading")) == ["pause", "cancel"] and caps(hub("queued")) == ["pause", "cancel"])
    check("capabilities per state: paused / stalled", caps(hub("paused")) == ["resume", "cancel"] and caps(hub("stalled")) == ["retry", "pause", "cancel"])
    check("capabilities per state: failed / cancelled / done", caps(hub("failed")) == ["retry"] and caps(hub("cancelled")) == ["retry"]
          and caps(hub("done")) == [] and caps(hub("done", dest_file="x")) == ["open", "reveal"])
    root = Path(HOME) / "Games"
    prov = {"capabilities": ["pause", "resume", "cancel", "retry", "open"], "open_roots": [str(root)]}
    pv = lambda s, **kw: dict({"owner": "provider", "state": s, "dest_file": None, "capabilities": None}, **kw)  # noqa: E731
    check("a provider's verbs are pruned by state", caps(pv("downloading"), prov) == ["pause", "cancel"] and caps(pv("paused"), prov) == ["resume", "cancel"]
          and caps(pv("failed"), prov) == ["retry"])
    check("open is offered only for a file inside its roots", caps(pv("done", dest_file=str(root / "g.exe")), prov) == ["open"]
          and caps(pv("done", dest_file=str(Path(HOME) / "elsewhere.exe")), prov) == [])
    check("a row can narrow what its provider declared", caps(pv("downloading", capabilities=["cancel"]), prov) == ["cancel"])
    check("a read-only provider offers nothing", caps(pv("downloading"), {"capabilities": []}) == [])

    status = service.status(store)
    check("status carries engines, settings and the daemon", status["ok"] and status["engines"]["http"]["available"] and status["settings"]["concurrency"]["total"] == 4 and "daemon" in status)
    store.upsert_provider_row("tool_x", "r1", {"title": "R1", "children": [{"name": "f1", "percent": 50}], "detail": [{"label": "A", "value": "B"}],
                                              "links": [{"label": "L", "kind": "url", "target": "https://example.com/"}], "state": "downloading"})
    detail = service.status(store, item_id="tool_x:r1")
    check("status(id) is one download in full", detail["ok"] and detail["item"]["children"][0]["name"] == "f1" and detail["item"]["detail"][0]["label"] == "A"
          and detail["item"]["events"] and detail["item"]["links"][0]["label"] == "L")
    check("status of an unknown id is an error", service.status(store, item_id="h_nope")["ok"] is False)

    check("settings read back", service.get_settings(store)["settings"]["concurrency"]["http"] == 3)
    res = service.update_settings({"concurrency.http": 1, "library_root": str(Path(HOME) / "NewLib")}, store=store)
    check("settings update", res["ok"] and res["settings"]["concurrency"]["http"] == 1 and res["settings"]["library_root"].endswith("NewLib"))
    check("an unknown setting is an error and changes nothing", service.update_settings({"nonsense": 1}, store=store)["ok"] is False and service.get_settings(store)["settings"]["concurrency"]["http"] == 1)
    check("a bad value is an error", service.update_settings({"concurrency.total": 99}, store=store)["ok"] is False)


def test_open_item():
    store = fresh_store()
    lib = Path(HOME) / "OpenLib"
    store.set_settings({"library_root": str(lib)})
    launched = []
    original = service.LAUNCH
    service.LAUNCH = lambda path, action: launched.append((str(path), action)) or True
    try:
        (lib / "Files").mkdir(parents=True)
        good = lib / "Files" / "ok.bin"
        good.write_bytes(b"x")
        a = store.add_item(engine="http", source_url=PUBLIC + "/ok.bin", title="ok.bin")[0]
        store.transition(a["id"], "probing")
        store.transition(a["id"], "done", dest_file=str(good))
        res = service.open_item(a["id"], "open", store=store)
        check("a finished file inside the library opens", res["ok"] and launched[-1] == (str(good), "open"))
        check("and can be revealed", service.open_item(a["id"], "reveal", store=store)["ok"] and launched[-1][1] == "reveal")
        check("an unknown action is refused", service.open_item(a["id"], "delete", store=store)["ok"] is False)

        outside = Path(HOME) / "outside.txt"
        outside.write_bytes(b"x")
        b = store.add_item(engine="http", source_url=PUBLIC + "/o", title="o")[0]
        store.update_item(b["id"], {"dest_file": str(outside)})
        before = len(launched)
        res = service.open_item(b["id"], "open", store=store)
        check("a file outside the library root is never opened", res["ok"] is False and "isn't inside" in res["error"] and len(launched) == before)
        traversal = lib / "Files" / ".." / ".." / "outside.txt"
        store.update_item(b["id"], {"dest_file": str(traversal)})
        check("a '..' path that escapes is refused too", service.open_item(b["id"], "open", store=store)["ok"] is False and len(launched) == before)

        c = store.add_item(engine="http", source_url=PUBLIC + "/m", title="m")[0]
        store.update_item(c["id"], {"dest_file": str(lib / "Files" / "missing.bin")})
        check("a file that has gone says so", "no longer there" in service.open_item(c["id"], "open", store=store)["error"])
        d = store.add_item(engine="http", source_url=PUBLIC + "/q", title="q")[0]
        check("a queued download has no file yet", "no file yet" in service.open_item(d["id"], "open", store=store)["error"])
        check("an unknown id is an error", service.open_item("h_nope", "open", store=store)["ok"] is False)

        games = Path(HOME) / "GamesRoot"
        games.mkdir()
        (games / "g.exe").write_bytes(b"x")
        store.register_provider("games", label="Games", capabilities=["open"], open_roots=[str(games)])
        store.upsert_provider_row("games", "g1", {"state": "done", "dest": str(games / "g.exe")})
        check("a provider's file inside its open_roots opens", service.open_item("games:g1", "open", store=store)["ok"])
        store.upsert_provider_row("noroots", "n1", {"state": "done", "dest": str(good)})
        check("a provider with no open_roots can never be opened", service.open_item("noroots:n1", "open", store=store)["ok"] is False)
        service.LAUNCH = lambda path, action: False
        check("a failed launch is reported, not raised", service.open_item(a["id"], "open", store=store)["ok"] is False)
    finally:
        service.LAUNCH = original


# ---------------------------------------------------------------- the handlers

def test_handlers():
    # The handlers use the default store (under HOME), like the real tools.
    check("dl_add with nothing asks which link", dh.tool_dl_add({}).get("needs_clarification") is True)
    dry = dh.tool_dl_add({"url": PUBLIC + "/a.zip", "dry_run": True})
    check("dry_run says which engine, queues nothing", dry["ok"] and dry["decisions"][0]["engine"] == "http" and Store().counts()["total"] == 0)
    dry = dh.tool_dl_add({"urls": ["magnet:?xt=urn:btih:abc"], "dry_run": True})
    check("dry_run explains a link it can't take", dry["decisions"][0]["ok"] is False and dry["decisions"][0]["problem"])
    res = dh.tool_dl_add({"url": PUBLIC + "/a.zip"})
    check("dl_add queues one link", res["ok"] and res["count"] == 1)

    original = ui_bridge.confirm
    asked = []
    try:
        ui_bridge.confirm = lambda *a, **k: asked.append((a, k)) or False
        many = [PUBLIC + "/m%d.zip" % i for i in range(6)]
        res = dh.tool_dl_add({"urls": many})
        check("more than 5 links ask first, defaulting to No", res.get("cancelled") is True and asked and asked[0][1].get("default") is False)
        check("and queue nothing when declined", Store().counts()["total"] == 1)
        ui_bridge.confirm = lambda *a, **k: True
        res = dh.tool_dl_add({"urls": many})
        check("and queue them all once confirmed", res["ok"] and res["count"] == 6)
        asked.clear()
        ui_bridge.confirm = lambda *a, **k: asked.append(1) or True
        dh.tool_dl_add({"urls": [PUBLIC + "/few1.zip", PUBLIC + "/few2.zip"]})
        check("a small batch doesn't ask", not asked)
    finally:
        ui_bridge.confirm = original

    listing = dh.tool_dl_list({"limit": "3", "summary": True})
    check("dl_list honours a limit (even given as text) and carries the summary", listing["ok"] and listing["count"] == 3 and "summary" in listing)
    check("a junk limit falls back instead of failing", dh.tool_dl_list({"limit": "lots"})["ok"])
    first = listing["items"][0]["id"]
    check("dl_status returns totals", dh.tool_dl_status({})["ok"] and "engines" in dh.tool_dl_status({}))
    check("dl_status(id) returns one download", dh.tool_dl_status({"id": first})["item"]["id"] == first)
    check("dl_pause by id", dh.tool_dl_pause({"id": first})["changed"] == [first])
    check("dl_resume by id", dh.tool_dl_resume({"id": first})["changed"] == [first])
    check("dl_cancel by id", dh.tool_dl_cancel({"id": first})["changed"] == [first])
    check("dl_retry by id", dh.tool_dl_retry({"id": first})["changed"] == [first])
    check("a control call with no selector is a clear error", "which download" in dh.tool_dl_pause({})["error"])
    check("dl_open on a queued download is an error, not an exception", dh.tool_dl_open({"id": first})["ok"] is False)
    check("dl_settings with no `set` reads", dh.tool_dl_settings({})["settings"]["concurrency"]["total"] == 4)
    check("dl_settings with `set` writes", dh.tool_dl_settings({"set": {"rate_limit_kbps": 250}})["settings"]["rate_limit_kbps"] == 250)
    check("dl_settings with a non-object `set` is an error", dh.tool_dl_settings({"set": "x"})["ok"] is False)
    check("dl_settings refuses an unknown key", dh.tool_dl_settings({"set": {"bogus": 1}})["ok"] is False)
    check("hub_doctor returns a report", "checks" in dh.tool_hub_doctor({}))
    check("hub_demo_provider rejects an unknown action", dh.tool_hub_demo_provider({"action": "explode"})["ok"] is False)
    started = dh.tool_hub_demo_provider({"action": "start"})
    check("hub_demo_provider start creates the rows (and warns the daemon is down)", started["ok"] and started["rows"] >= 6 and "daemon" in started.get("note", ""))
    check("status reports it running", dh.tool_hub_demo_provider({"action": "status"})["running"] is True)
    check("stop and clear work", dh.tool_hub_demo_provider({"action": "stop"})["ok"] and dh.tool_hub_demo_provider({"action": "clear"})["removed"] >= 6)

    bomb = dh._svc
    dh._svc = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        check("a handler never raises", dh.tool_dl_list({})["ok"] is False and dh.tool_dl_add({"url": "https://x"})["ok"] is False
              and dh.tool_dl_status({})["ok"] is False and dh.tool_dl_pause({"id": "x"})["ok"] is False)
    finally:
        dh._svc = bomb


# ---------------------------------------------------------------- hub_client

def test_hub_client():
    store = fresh_store()
    hub_client.set_store_factory(lambda: store)
    try:
        item = hub_client.begin("model_fetch", "llama", title="llama.gguf", kind="file", total_bytes=1000)
        check("begin creates a namespaced row", item.ok and item.id == "model_fetch:llama" and store.get_item("model_fetch:llama")["title"] == "llama.gguf"
              and store.get_item("model_fetch:llama")["bytes_total"] == 1000)
        again = hub_client.begin("model_fetch", "llama", title="llama.gguf (renamed)")
        check("begin again re-attaches to the same row", store.get_item("model_fetch:llama")["title"] == "llama.gguf (renamed)"
              and len([r for r in store.list_items(limit=100) if r["provider"] == "model_fetch"]) == 1)
        item.progress(bytes_done=100)
        check("a progress report right after begin is coalesced (one write a second)", store.get_item("model_fetch:llama")["bytes_done"] is None)
        item.progress(bytes_done=300, state="downloading")
        row = store.get_item("model_fetch:llama")
        check("a state change is written at once, carrying the pending values", row["state"] == "downloading" and row["bytes_done"] == 300 and row["percent"] == 30.0)
        item.progress(percent=None, message="no idea how far")
        item.flush()
        check("percent=None means indeterminate", store.get_item("model_fetch:llama")["percent"] is None)
        item.progress(bytes_done=500)
        item.phase("verifying", index=2, count=2)
        row = store.get_item("model_fetch:llama")
        check("a phase is written at once, as post-processing, with pending progress", row["state"] == "postprocessing" and row["phase"] == "verifying"
              and row["phase_index"] == 2 and row["bytes_done"] == 500)
        item.done(path=str(Path(HOME) / "x.gguf"))
        row = store.get_item("model_fetch:llama")
        check("done finishes the row at 100 percent", row["state"] == "done" and row["percent"] == 100.0)

        failing = hub_client.begin("model_fetch", "bad", title="bad")
        failing.fail("checksum mismatch")
        check("fail records the error", store.get_item("model_fetch:bad")["state"] == "failed" and store.get_item("model_fetch:bad")["error"] == "checksum mismatch")
        failing.cancel()
        check("cancel marks it cancelled", store.get_item("model_fetch:bad")["state"] == "cancelled")
        check("update() changes a row by ids", hub_client.update("model_fetch", "bad", message="again") and store.get_item("model_fetch:bad")["message"] == "again")
        check("finish() ends a row by ids", hub_client.finish("model_fetch", "bad", state="done") and store.get_item("model_fetch:bad")["state"] == "done")
        check("update() on a missing row is False, not an error", hub_client.update("model_fetch", "ghost", message="x") is False)

        for bad in ("http", "hub", "Bad Id", "a:b", "", None, 5):
            handle = hub_client.begin(bad, "x", title="t")
            check("provider %r is refused and the handle is inert" % (bad,), handle.ok is False and handle.progress(bytes_done=1) is False and handle.done() is False)
        check("nothing was written for them", all(r["provider"] == "model_fetch" for r in store.list_items(limit=100)))
        check("a missing local id is refused", hub_client.begin("model_fetch", "", title="t").ok is False)

        check("register_provider works", hub_client.register_provider("fancy", label="Fancy", capabilities=["pause", "cancel"], controls={"pause": "fancy_pause"})
              and store.get_provider("fancy")["capabilities"] == ["pause", "cancel"])
        check("a reserved provider id is refused", hub_client.register_provider("stremio") is False)

        hub_client._buckets.clear()
        allowed = sum(1 for _ in range(40) if hub_client._allow("chatty", 100.0))
        check("a provider is held to 20 writes a second", allowed == hub_client.MAX_REPORTS_PER_S)
        check("and the bucket refills with time", hub_client._allow("chatty", 101.0))

        # --- the store being unavailable: reports are spooled, then applied
        def broken():
            raise RuntimeError("database is locked")

        hub_client.set_store_factory(broken)
        inbox = paths.inbox_dir()
        for stale in inbox.glob("*.json") if inbox.is_dir() else []:
            stale.unlink()
        ghost = hub_client.begin("spool_tool", "s1", title="Spooled", state="downloading")
        ghost.progress(bytes_done=5, bytes_total=10, state="downloading")
        ghost.flush()
        spooled = sorted(inbox.glob("*.json"))
        check("with the store down, reports are spooled to the inbox (and nothing raised)", len(spooled) >= 2)
        check("a spooled report is a plain JSON file the daemon can read", json.loads(spooled[0].read_text(encoding="utf-8"))["provider"] == "spool_tool")
        hub_client.set_store_factory(lambda: store)
        applied = hub_client.drain_inbox(store)
        check("drain_inbox applies them in order", applied == len(spooled) and store.get_item("spool_tool:s1")["bytes_done"] == 5)
        check("and empties the inbox", list(inbox.glob("*.json")) == [])
        check("draining an empty or missing inbox is fine", hub_client.drain_inbox(store) == 0)
    finally:
        hub_client.set_store_factory(None)


# ---------------------------------------------------------------- demo

def test_demo():
    first = demo.rows_at(0)
    check("the demo has seven rows at the start", set(first) == {"climb", "indeterminate", "postproc", "stalled", "failing", "game", "silent"} and all(v for v in first.values()))
    check("the climbing row reports only bytes, so the hub derives percent / speed / ETA", "percent" not in first["climb"] and "speed_bps" not in first["climb"])
    check("it climbs", demo.rows_at(30)["climb"]["bytes_done"] > demo.rows_at(10)["climb"]["bytes_done"])
    check("then verifies at 100 percent", demo.rows_at(62)["climb"]["state"] == "postprocessing" and demo.rows_at(62)["climb"]["phase"] == "verifying")
    check("then is done", demo.rows_at(80)["climb"]["state"] == "done")
    check("the failing row fails with a message", demo.rows_at(20)["failing"]["state"] == "failed" and demo.rows_at(20)["failing"]["error"])
    check("the indeterminate row has no percent at all", demo.rows_at(5)["indeterminate"]["percent"] is None)
    check("the silent row stops reporting", demo.rows_at(5)["silent"] and demo.rows_at(11)["silent"] is None)
    check("the stalled row is stalled", demo.rows_at(5)["stalled"]["state"] == "stalled")
    check("the game row carries a link and children", demo.rows_at(5)["game"]["links"] and demo.rows_at(5)["game"]["children"])

    clock = Clock()
    store = fresh_store(clock)
    check("tick does nothing while the demo is off", demo.tick(store, clock()) == 0)
    out = demo.start(store)
    check("start writes the rows at once", out["ok"] and out["rows"] == 7)
    rows = {r["id"]: r for r in store.list_items(provider="demo", limit=50)}
    check("they are the demo provider's, read-only", len(rows) == 7 and all(r["owner"] == "provider" for r in rows.values())
          and all(service.present_item(r, {p["id"]: p for p in store.list_providers()}, clock())["read_only"] for r in rows.values()))
    for _ in range(45):
        clock.advance(1)
        demo.tick(store, clock())
    store.mark_stale(now=clock())
    silent = store.get_item("demo:silent")
    check("the silent row goes stale (not failed) while the others keep reporting",
          silent["stale"] and silent["state"] == "downloading" and not store.get_item("demo:climb")["stale"] and not store.get_item("demo:indeterminate")["stale"])
    check("the climbing row has a derived percent and speed (the hub's estimates)", "percent" in store.get_item("demo:climb")["derived"] and "speed" in store.get_item("demo:climb")["derived"])
    check("the failing row ended failed", store.get_item("demo:failing")["state"] == "failed")
    for _ in range(30):
        clock.advance(1)
        demo.tick(store, clock())
    check("the climbing row finishes", store.get_item("demo:climb")["state"] == "done" and store.get_item("demo:climb")["percent"] == 100.0)
    check("status says it is running", demo.status(store)["running"] and demo.status(store)["rows"] == 7)
    stopped = demo.stop(store)
    check("stop cancels what was still unfinished and stops ticking", stopped["cancelled"] >= 3 and store.get_item("demo:indeterminate")["state"] == "cancelled" and demo.tick(store, clock()) == 0)
    check("restarting replaces the rows", demo.start(store)["rows"] == 7 and store.get_item("demo:indeterminate")["state"] == "downloading")
    check("clear removes them all", demo.clear(store)["removed"] == 7 and store.list_items(provider="demo", limit=50) == [])


# ---------------------------------------------------------------- doctor

def test_doctor():
    clock = Clock()
    store = fresh_store(clock)
    report = doctor.run(store)
    by = {c["name"]: c for c in report["checks"]}
    check("the report has ok, summary and checks", set(report) == {"ok", "summary", "checks"} and report["checks"])
    check("a healthy empty hub is ok", report["ok"])
    check("the store is checked", by["store"]["status"] == "ok")
    check("a daemon that isn't running is a warning", by["daemon"]["status"] == "warn" and "Daemons" in by["daemon"]["detail"])
    check("the HTTP engine is reported ready", by["engine http"]["status"] == "ok")
    check("the library root is checked", by["library root"]["status"] in ("ok", "warn"))
    check("the version is reported", "hub version" in by)

    item = store.add_item(engine="http", source_url=PUBLIC + "/x", title="x")[0]
    store.transition(item["id"], "probing", expect="queued")
    report = doctor.run(store)
    by = {c["name"]: c for c in report["checks"]}
    check("running downloads with a dead daemon is a failure that names the fix", by["daemon"]["status"] == "fail" and not report["ok"] and "Download hub" in by["daemon"]["detail"])
    store.daemon_beat(os.getpid(), clock())
    report = doctor.run(store)
    check("a live daemon is ok and the failure goes away", {c["name"]: c for c in report["checks"]}["daemon"]["status"] == "ok" and report["ok"])

    store.register_provider("sick", label="Sick")
    store.note_provider_health("sick", ok=False, error="the vendor CLI isn't installed")
    store.set_provider_enabled("sick", True)
    by = {c["name"]: c for c in doctor.run(store)["checks"]}
    check("a provider's last error is a warning", by["provider sick"]["status"] == "warn" and "vendor CLI" in by["provider sick"]["detail"])
    store.register_provider("off", label="Off")
    store.set_provider_enabled("off", False)
    check("a provider switched off is noted as the owner's choice", {c["name"]: c for c in doctor.run(store)["checks"]}["provider off"]["detail"].startswith("switched off"))

    class Extra:
        name = "extra"
        label = "Extra"
        supports_pause = True

        def available(self):
            return False, "ffmpeg isn't installed"

        def doctor(self):
            return [{"name": "extra: ffmpeg", "status": "warn", "detail": "not on PATH"}]

        def run(self, item, ctx):
            raise NotImplementedError

    engines.load()
    engines.ENGINES["extra"] = Extra()
    try:
        by = {c["name"]: c for c in doctor.run(store)["checks"]}
        check("an engine's own prerequisites appear under its name", by["engine extra"]["status"] == "warn" and "ffmpeg" in by["engine extra"]["detail"])
        check("and an engine can add its own checks", by["extra: ffmpeg"]["detail"] == "not on PATH")
    finally:
        engines.ENGINES.pop("extra", None)
    engines.LOAD_ERRORS["broken_mod"] = "ImportError: nope"
    try:
        report = doctor.run(store)
        check("an engine module that failed to load is a failure", not report["ok"] and any(c["name"] == "engine module broken_mod" and c["status"] == "fail" for c in report["checks"]))
    finally:
        engines.LOAD_ERRORS.pop("broken_mod", None)

    inbox = paths.inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "0001-x.json").write_text("{}", encoding="utf-8")
    try:
        check("spooled reports waiting are mentioned", any(c["name"] == "inbox" for c in doctor.run(store)["checks"]))
    finally:
        (inbox / "0001-x.json").unlink()


try:
    test_tool_file()
    test_add_items()
    test_listing_status_settings()
    test_open_item()
    test_handlers()
    test_hub_client()
    test_demo()
    test_doctor()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
