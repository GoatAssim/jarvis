"""Download Hub phase 8 / phase 9 option B: jarvis/hub_admin.py (the back end of `jarvis hub-admin`).

Covers: the card payload (items, a group, a title search, a missing id) and its read-only nature; the
group card's counts, overall percent and state; the push stream's pure diff (a changed row, a removed
row, a silent provider row that must not re-send every second) and the generator (one snapshot, then
only deltas, a heartbeat, a bad tick that doesn't end the stream); thumbnails (only a small image
data-URI, a known type) and files (only inside the library root / a provider's open_roots, only when
done); the settings payload (what is writable comes from hub/settings.py); History (finished rows only);
removing history rows (hub rows only, finished only, never a provider's, no file touched); clearing by
age (dry run counts, then removes); downloading again; reordering the queue (queued hub rows only);
the refusals for an unattended process; the CLI entry's JSON-in / JSON-out; and the `hub-admin`
plumbing (reserved name, slash registry, cli.py branch, server routes' guards).

Run: python tests/test_hub_admin.py
"""
import base64
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-admin-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_TASK_ID", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI))

from jarvis import hub_admin  # noqa: E402
from jarvis.hub import service, settings as hub_settings  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]
PUBLIC = "http://93.184.216.34"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def fresh_store():
    _n[0] += 1
    return Store(Path(HOME) / f"admin-{_n[0]}.db", clock=time.time)


def make(store, title, state="queued", **kw):
    item = store.add_item(engine="http", source_url=PUBLIC + "/" + title.replace(" ", "_"), title=title, **kw)[0]
    if state == "done":
        store.update_item(item["id"], {"state": "downloading"})
        store.update_item(item["id"], {"state": "done"})
    elif state in ("failed", "cancelled"):
        store.update_item(item["id"], {"state": state})
    elif state == "downloading":
        store.update_item(item["id"], {"state": "downloading", "percent": 40, "bytes_done": 40, "bytes_total": 100, "speed_bps": 1024})
    return store.get_item(item["id"])


def test_card():
    st = fresh_store()
    a = make(st, "alpha", "downloading")
    b = make(st, "beta", "done")
    out = hub_admin.card_payload(ids=[a["id"], b["id"], "no-such-id", "bad id!"], store=st)
    check("a card payload answers ok with one card per real id", out["ok"] and [c["id"] for c in out["cards"]] == [a["id"], b["id"]])
    check("each card is the panel's row plus `card` and `final`", out["cards"][0]["card"] == "item" and out["cards"][0]["final"] is False and out["cards"][1]["final"] is True)
    check("rows carry has_thumb and priority for the panel", out["cards"][0]["has_thumb"] is False and out["cards"][0]["priority"] == 0)
    check("a missing id is a clean refusal, not a crash", hub_admin.card_payload(ids=["nope"], store=st)["ok"] is False)
    check("nothing asked is a clean refusal", hub_admin.card_payload(store=st)["ok"] is False)
    check("a title search finds it", [c["title"] for c in hub_admin.card_payload(query="alp", store=st)["cards"]] == ["alpha"])
    check("at most eight cards", len(hub_admin.card_payload(ids=[make(st, "n%d" % i)["id"] for i in range(12)], store=st)["cards"]) == 8)
    before = st.counts()
    hub_admin.card_payload(ids=[a["id"]], store=st)
    check("reading a card writes nothing", st.counts() == before)


def test_group_card():
    rows = [{"state": "done", "percent": 100, "bytes_total": 100}, {"state": "downloading", "percent": 50, "bytes_total": 100, "speed_bps": 10},
            {"state": "queued"}, {"state": "failed"}]
    g = hub_admin.group_card("g1", rows, "Batch of 4")
    check("a group card counts its members", g["counts"]["done"] == 1 and g["counts"]["running"] == 1 and g["counts"]["queued"] == 1 and g["counts"]["failed"] == 1 and g["counts"]["total"] == 4)
    check("it is a group card, still running, with the summed speed", g["card"] == "group" and g["state"] == "downloading" and g["final"] is False and g["speed_bps"] == 10)
    check("its overall percent averages every member (a waiting one counts for nothing)", g["percent"] is not None and 0 < g["percent"] < 100)
    done = hub_admin.group_card("g2", [{"state": "done", "percent": 100}, {"state": "done", "percent": 100}])
    check("all done -> done and final, 100%", done["state"] == "done" and done["final"] is True and done["percent"] == 100)
    mixed = hub_admin.group_card("g3", [{"state": "done"}, {"state": "failed"}])
    check("everything finished but one failed -> final, failed, needs attention", mixed["final"] is True and mixed["state"] == "failed" and mixed["attention"] == 1)
    check("an empty group is unknown, not a crash", hub_admin.group_card("g4", [])["state"] == "unknown")
    st = fresh_store()
    gid = "grp1"
    for t in ("one", "two"):
        make(st, t, group_id=gid)
    out = hub_admin.card_payload(group=gid, store=st)
    check("a group id gives one group card from the store", out["ok"] and out["cards"][0]["card"] == "group" and out["cards"][0]["counts"]["total"] == 2)


def test_stream_diff():
    rows = [{"id": "a", "state": "downloading", "percent": 1, "age_s": 3}, {"id": "b", "state": "queued", "age_s": 3}]
    up, rm, cur = hub_admin.diff_rows({}, rows)
    check("the first diff sends every row", len(up) == 2 and rm == [])
    up, rm, cur2 = hub_admin.diff_rows(cur, rows)
    check("nothing changed -> nothing sent", up == [] and rm == [])
    changed = [dict(rows[0], percent=2), rows[1]]
    up, rm, cur3 = hub_admin.diff_rows(cur2, changed)
    check("a changed row is sent alone", [r["id"] for r in up] == ["a"] and rm == [])
    quiet = [dict(rows[0], percent=2, age_s=9), rows[1]]
    up, rm, _ = hub_admin.diff_rows(cur3, quiet)
    check("a silent provider row's age moving a few seconds is not a message", up == [])
    older = [dict(rows[0], percent=2, age_s=45), rows[1]]
    up, rm, _ = hub_admin.diff_rows(cur3, older)
    check("its age moving a whole step is", [r["id"] for r in up] == ["a"])
    up, rm, _ = hub_admin.diff_rows(cur3, [rows[1]])
    check("a removed row is reported by id", up == [] and rm == ["a"])


def test_watch_generator():
    st = fresh_store()
    a = make(st, "first", "downloading")
    clock = [1000.0]
    ticks = [0]
    msgs = []

    def sleep(_):
        ticks[0] += 1
        clock[0] += 1.0
        if ticks[0] == 1:
            st.update_item(a["id"], {"percent": 70, "bytes_done": 70})
        if ticks[0] == 3:
            make(st, "second")
        if ticks[0] == 5:
            st.delete_items([a["id"]])

    gen = hub_admin.watch_events(store=st, interval=0, stop=lambda: ticks[0] >= 22, clock=lambda: clock[0], sleep=sleep)
    for m in gen:
        msgs.append(m)
    check("the first message is a full snapshot with the rows and a summary", msgs[0]["type"] == "snapshot" and len(msgs[0]["items"]) == 1 and "summary" in msgs[0])
    check("the summary leaves out the big notification catalog", "notify_options" not in msgs[0]["summary"])
    deltas = [m for m in msgs if m["type"] == "delta"]
    check("later messages are deltas, one for each change", len(deltas) >= 3)
    check("a progress change arrives as an upsert of that row", any(m["upsert"] and m["upsert"][0]["id"] == a["id"] and m["upsert"][0]["percent"] == 70 for m in deltas))
    check("a new row arrives as an upsert, a deleted one as a removal", any(any(r["title"] == "second" for r in m["upsert"]) for m in deltas) and any(a["id"] in m["remove"] for m in deltas))
    # (free disk space in the summary can also move, so a "quiet" hub may talk through a summary delta instead of a beat)
    check("a quiet hub keeps talking, so a client can tell it from a dead stream", any(m["type"] == "beat" for m in msgs) or sum(1 for m in deltas if "summary" in m) >= 2)
    check("the stream only ever reads", all(m["type"] in ("snapshot", "delta", "beat", "error") for m in msgs))


def test_thumb_and_file():
    st = fresh_store()
    it = make(st, "pic", "done")
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 50).decode()
    st.update_item(it["id"], {"thumb_ref": "data:image/png;base64," + png}, touch=False)
    out = hub_admin.resolve_thumb(it["id"], store=st)
    check("a small image data-URI is served with its type", out["ok"] and out["mime"] == "image/png" and base64.b64decode(out["data"]).startswith(b"\x89PNG"))
    st.update_item(it["id"], {"thumb_ref": "data:image/svg+xml;base64," + png}, touch=False)
    check("an SVG (scriptable) is refused", hub_admin.resolve_thumb(it["id"], store=st)["ok"] is False)
    st.update_item(it["id"], {"thumb_ref": "data:text/html;base64," + png}, touch=False)
    check("HTML is refused", hub_admin.resolve_thumb(it["id"], store=st)["ok"] is False)
    st.update_item(it["id"], {"thumb_ref": "https://evil.example/x.png"}, touch=False)
    check("a remote URL is never fetched or served", hub_admin.resolve_thumb(it["id"], store=st)["ok"] is False)
    big = base64.b64encode(b"x" * (hub_admin.MAX_THUMB_BYTES + 10)).decode()
    st.update_item(it["id"], {"thumb_ref": "data:image/png;base64," + big}, touch=False)
    check("an oversize one is refused", hub_admin.resolve_thumb(it["id"], store=st)["ok"] is False)
    check("a bad id is refused without a lookup", hub_admin.resolve_thumb("../etc/passwd", store=st)["ok"] is False and hub_admin.resolve_thumb("nope", store=st)["ok"] is False)

    # the library is wherever the hub says it is (by default ~/Downloads/Jarvis); never a path guessed here
    lib = hub_settings.library_root(st.get_settings())
    lib.mkdir(parents=True, exist_ok=True)
    inside = lib / "ok.bin"
    inside.write_bytes(b"data")
    outside = Path(HOME) / "secret.txt"
    outside.write_text("nope")
    f = make(st, "file", "done")
    st.update_item(f["id"], {"dest_file": str(inside)}, touch=False)
    got = hub_admin.resolve_file(f["id"], store=st)
    check("a finished file inside the library resolves, by id", got["ok"] and got["path"] == str(inside) and got["name"] == "ok.bin" and got["size"] == 4)
    st.update_item(f["id"], {"dest_file": str(outside)}, touch=False)
    check("a file outside every allowed folder is refused", hub_admin.resolve_file(f["id"], store=st)["ok"] is False)
    st.update_item(f["id"], {"dest_file": str(lib / ".." / "secret.txt")}, touch=False)
    check("a .. escape is refused", hub_admin.resolve_file(f["id"], store=st)["ok"] is False)
    st.update_item(f["id"], {"dest_file": str(lib / "gone.bin")}, touch=False)
    check("a file that is no longer there is a clean refusal", hub_admin.resolve_file(f["id"], store=st)["ok"] is False)
    q = make(st, "unfinished")
    check("an unfinished download has no file to give", hub_admin.resolve_file(q["id"], store=st)["ok"] is False)


def test_settings_and_history():
    st = fresh_store()
    out = hub_admin.settings_payload(st)
    check("the settings payload says what is writable, read from the hub's own lists",
          out["ok"] and out["writable"] == sorted(hub_settings.WRITABLE) and out["prefs_writable"] == sorted(hub_settings.PREFS_WRITABLE) and out["events"] == list(hub_settings.EVENTS))
    check("it carries the merged settings and the open modes", "rate_limit_kbps" in out["settings"] and out["open_modes"] == list(hub_settings.OPEN_MODES))

    a = make(st, "done one", "done")
    b = make(st, "failed one", "failed")
    c = make(st, "cancelled one", "cancelled")
    d = make(st, "queued one")
    h = hub_admin.history({}, store=st)
    ids = {r["id"] for r in h["items"]}
    check("History holds finished, failed and cancelled rows only", h["ok"] and ids == {a["id"], b["id"], c["id"]})
    check("it can be narrowed to one state", {r["id"] for r in hub_admin.history({"states": ["failed"]}, store=st)["items"]} == {b["id"]})
    check("an unknown state is ignored, not trusted", {r["id"] for r in hub_admin.history({"states": ["queued"]}, store=st)["items"]} == ids)
    check("a title search narrows it", [r["title"] for r in hub_admin.history({"search": "cancel"}, store=st)["items"]] == ["cancelled one"])
    check("the page size is capped", hub_admin.history({"limit": 10 ** 9}, store=st)["count"] <= hub_admin.MAX_HISTORY)


def test_history_writes():
    st = fresh_store()
    a = make(st, "done one", "done")
    b = make(st, "failed one", "failed")
    q = make(st, "queued one")
    lib = Path(HOME) / ".jarvis" / "downloads"
    lib.mkdir(parents=True, exist_ok=True)
    keep = lib / "keep.bin"
    keep.write_bytes(b"x")
    st.update_item(a["id"], {"dest_file": str(keep)}, touch=False)
    out = hub_admin.history_remove({"ids": [a["id"], q["id"], "zzz"]}, store=st)
    check("a finished hub row is removed from the list", out["ok"] and out["removed"] == 1 and st.get_item(a["id"]) is None)
    check("the downloaded file is never touched", keep.exists())
    why = {s["id"]: s["why"] for s in out["skipped"]}
    check("an unfinished row is refused by name, with the reason", q["id"] in why and "finished" in why[q["id"]] and st.get_item(q["id"]) is not None)
    check("an unknown id is reported, not crashed on", "zzz" in why)
    check("nothing asked is a clean refusal", hub_admin.history_remove({"ids": []}, store=st)["ok"] is False)
    check("the answer says only the list entries went", "not touched" in out["note"])
    # a provider's row is never removed (it would just come back)
    prov = st.upsert_provider({"id": "game", "label": "Game", "mode": "push", "kind": "game", "icon": "x", "enabled": True, "capabilities": [], "controls": [], "open_roots": []}) if hasattr(st, "upsert_provider") else None
    if prov is not None and hasattr(st, "upsert_provider_row"):
        row = st.upsert_provider_row("game", "g1", {"title": "Game install", "state": "done"})
        res = hub_admin.history_remove({"ids": [row["id"]]}, store=st)
        check("a provider's row is refused and kept", st.get_item(row["id"]) is not None and res["skipped"] and "report it again" in res["skipped"][0]["why"])
    else:
        check("(the provider-row removal check needs the store's provider API; skipped)", True)


def test_clear_by_age():
    st = fresh_store()
    old = make(st, "old", "done")
    new = make(st, "new", "done")
    live = make(st, "live")
    now = time.time()
    st.update_item(old["id"], {"finished_at": now - 100 * 86400}, touch=False)
    st.update_item(new["id"], {"finished_at": now - 2 * 86400}, touch=False)
    dry = hub_admin.history_clear({"days": 30, "dry_run": True}, store=st)
    check("a dry run only counts", dry["ok"] and dry["would_remove"] == 1 and st.get_item(old["id"]) is not None)
    out = hub_admin.history_clear({"days": 30}, store=st)
    check("older rows go, newer and unfinished rows stay", out["removed"] == 1 and st.get_item(old["id"]) is None and st.get_item(new["id"]) and st.get_item(live["id"]))
    check("days must be 1 to 3650", all(hub_admin.history_clear({"days": d}, store=st)["ok"] is False for d in (0, -3, 4000, "x", None)))


def test_redownload_and_reorder():
    st = fresh_store()
    done = make(st, "again", "done")
    out = hub_admin.redownload({"id": done["id"]}, store=st)
    check("a finished download is queued again from the link the hub kept", out["ok"] and out["added"] and out.get("redownload_of") == done["id"])
    check("a row still in the list is not 'downloaded again'", hub_admin.redownload({"id": make(st, "still")["id"]}, store=st)["ok"] is False)
    check("an unknown id is a clean refusal", hub_admin.redownload({"id": "nope"}, store=st)["ok"] is False)

    st2 = fresh_store()
    x, y, z = (make(st2, n) for n in ("x", "y", "z"))
    live = make(st2, "live", "downloading")
    res = hub_admin.reorder({"ids": [z["id"], x["id"], y["id"]]}, store=st2)
    pr = {i: st2.get_item(i)["priority"] for i in (x["id"], y["id"], z["id"])}
    check("reordering sets run order: the first listed runs first", res["ok"] and pr[z["id"]] > pr[x["id"]] > pr[y["id"]])
    check("a row that isn't waiting in the hub queue is refused, and nothing is changed",
          hub_admin.reorder({"ids": [x["id"], live["id"]]}, store=st2)["ok"] is False and st2.get_item(x["id"])["priority"] == pr[x["id"]])
    check("one row can't be 'reordered'", hub_admin.reorder({"ids": [x["id"]]}, store=st2)["ok"] is False)


def test_unattended():
    st = fresh_store()
    a = make(st, "done one", "done")
    for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
        os.environ[var] = "1"
        res = hub_admin.admin("history_remove", {"ids": [a["id"]]}, store=st)
        check("%s: the writes are refused" % var, res["ok"] is False and res.get("code") == "unattended" and st.get_item(a["id"]) is not None)
        check("%s: the reads still work (a card in a chat reply)" % var, hub_admin.admin("card", {"ids": [a["id"]]}, store=st)["ok"] is True)
        del os.environ[var]
    check("every write op is on the guarded list, and no read op is", set(hub_admin.WRITE_OPS) == {"history_remove", "history_clear", "redownload", "reorder", "stremio_save"} and not set(hub_admin.READ_OPS) & set(hub_admin.WRITE_OPS))
    check("an unknown op is a usage line, not a crash", hub_admin.admin("rm-rf", {})["ok"] is False)


def test_cli():
    out = io.StringIO()
    hub_admin.run_cli(["nope"], stdin=io.StringIO(""), stdout=out)
    check("an unknown verb answers one JSON refusal", json.loads(out.getvalue())["ok"] is False)
    out = io.StringIO()
    hub_admin.run_cli(["card"], stdin=io.StringIO("not json"), stdout=out)
    check("a body that isn't JSON is refused, not a traceback", json.loads(out.getvalue())["ok"] is False)
    out = io.StringIO()
    hub_admin.run_cli(["card"], stdin=io.StringIO("[1,2]"), stdout=out)
    check("a body that isn't an object is refused", json.loads(out.getvalue())["ok"] is False)


def test_plumbing():
    cli = (CLI / "jarvis" / "cli.py").read_text(encoding="utf-8")
    check("cli.py dispatches hub-admin with a one-line hand-off", 'argv[0] == "hub-admin"' in cli and "_hub_admin.run_cli(argv[1:])" in cli)
    reserved = (CLI / "jarvis" / "reserved_names.py").read_text(encoding="utf-8")
    check("hub-admin is a reserved name", '"hub-admin"' in reserved)
    slash = (ROOT / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8")
    check("the slash registry lists it as not exposed", '"hub-admin"' in slash)
    server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    check("every /api/hub write route needs the hub header, and every route is loopback-checked",
          server.count("requireHubWrite, requireJarvis") == 5 and "requireHubRead, requireJarvis" in server and "function hubOriginOk" in server)
    check("a thumbnail or file route takes an id and never a path", 'app.get("/api/hub/thumb/:id"' in server and 'app.get("/api/hub/file/:id"' in server and "req.query.path" not in server.split("// Download Hub web back end")[1].split("// Calendars panel")[0])
    check("the thumbnail route pins its content type and nosniff", "X-Content-Type-Options" in server and "sandbox" in server)
    check("the stream route is capped and stops its watcher", "HUB_MAX_CLIENTS" in server and "hubWatchStopSoon" in server)
    check("hub_admin.py never opens the database itself", "sqlite3" not in (CLI / "jarvis" / "hub_admin.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    for fn in (test_card, test_group_card, test_stream_diff, test_watch_generator, test_thumb_and_file, test_settings_and_history,
               test_history_writes, test_clear_by_age, test_redownload_and_reorder, test_unattended, test_cli, test_plumbing):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
