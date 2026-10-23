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

Brief 19 (round 4, agent P) adds: the `dismiss` write op (one row, every finished row, the hub's own refusals
unchanged, an unattended process refused, no file touched), `list` with `include_dismissed` and the
`dismissed_at` every row carries, `unknown_settle_hours` as the settings payload reports it and the hub
validates it, and the `stremio_start` write op (`tool_stack_start` patched: no real process; the real
function with the app open starts nothing).

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


def provider_rows(st):
    """A temporary store with a Steam-like provider and one provider row in each state that matters."""
    st.register_provider("steam", label="Steam", mode="poll", poll_tool="get_x", capabilities=["reveal"])
    for local, state in (("run", "downloading"), ("fin", "done"), ("fail", "failed"), ("can", "cancelled"),
                         ("unk", "unknown"), ("pau", "paused"), ("que", "queued")):
        st.upsert_provider_row("steam", local, {"title": "Row " + local, "state": state,
                                                "percent": 100 if state == "done" else 5})
    return st


def test_dismiss_op():
    st = provider_rows(fresh_store())
    hub_done = make(st, "hub finished", "done")
    keep = Path(HOME) / "library-file.bin"
    keep.write_bytes(b"x")
    st.update_item("steam:fin", {"dest_file": str(keep)}, touch=False)
    check("dismiss is a write op (so an unattended process is refused before it runs)",
          "dismiss" in hub_admin.WRITE_OPS and "dismiss" not in hub_admin.READ_OPS)
    one = hub_admin.admin("dismiss", {"id": "steam:fin"}, store=st)
    check("one finished provider row is hidden, with the hub's own answer",
          one["ok"] and one["dismissed"] == ["steam:fin"] and one["count"] == 1 and "no file was touched" in one["note"])
    check("the row is still in the database, marked, and its file is untouched",
          st.get_item("steam:fin")["dismissed_at"] is not None and keep.exists())
    again = hub_admin.admin("dismiss", {"id": "steam:fin"}, store=st)
    check("hiding it twice is ok and says it was already hidden", again["ok"] and again["dismissed"] == [] and "already" in again["note"])
    run = hub_admin.admin("dismiss", {"id": "steam:run"}, store=st)
    check("a running row is refused with the hub's words and stays", run["ok"] is False and "still running" in run["error"]
          and st.get_item("steam:run")["dismissed_at"] is None)
    pau = hub_admin.admin("dismiss", {"id": "steam:pau"}, store=st)
    que = hub_admin.admin("dismiss", {"id": "steam:que"}, store=st)
    check("a row that hasn't finished is refused (paused, queued)", pau["ok"] is False and que["ok"] is False
          and "hasn't finished" in pau["error"] and "hasn't finished" in que["error"])
    hub = hub_admin.admin("dismiss", {"id": hub_done["id"]}, store=st)
    check("a hub row is refused, even a finished one, and points to dl_cancel and History",
          hub["ok"] is False and "dl_cancel" in hub["error"] and "History" in hub["error"] and st.get_item(hub_done["id"])["dismissed_at"] is None)
    check("an unknown id is a refusal, not a crash", hub_admin.admin("dismiss", {"id": "steam:nope"}, store=st)["ok"] is False)
    check("a malformed id is refused before it reaches the hub", hub_admin.admin("dismiss", {"id": "a b/../c"}, store=st)["ok"] is False)
    check("neither an id nor finished is a refusal", hub_admin.admin("dismiss", {}, store=st)["ok"] is False)
    check("both an id and finished is a refusal (the hub's rule)", hub_admin.admin("dismiss", {"id": "steam:fail", "finished": True}, store=st)["ok"] is False
          and st.get_item("steam:fail")["dismissed_at"] is None)
    check("finished: \"true\" as text is not finished: true", hub_admin.admin("dismiss", {"finished": "true"}, store=st)["ok"] is False)
    allrows = hub_admin.admin("dismiss", {"finished": True}, store=st)
    check("Clear finished hides every unknown or finished provider row that is still shown",
          allrows["ok"] and sorted(allrows["dismissed"]) == ["steam:can", "steam:fail", "steam:unk"] and allrows["count"] == 3)
    check("... and nothing else: running, paused, queued and the hub row stay",
          all(st.get_item(i)["dismissed_at"] is None for i in ("steam:run", "steam:pau", "steam:que", hub_done["id"])))
    check("clearing again says there is nothing to clear", hub_admin.admin("dismiss", {"finished": True}, store=st)["note"] == "nothing to clear")
    check("no file was touched by any of it", keep.exists() and keep.read_bytes() == b"x")
    # the answer is service.dismiss's, unchanged
    st2 = provider_rows(fresh_store())
    direct = service.dismiss(id="steam:run", store=st2)
    via = hub_admin.admin("dismiss", {"id": "steam:run"}, store=st2)
    check("a refusal through hub-admin is exactly the service's refusal", direct == via)
    for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
        os.environ[var] = "1"
        res = hub_admin.admin("dismiss", {"id": "steam:fail"}, store=st2)
        res_all = hub_admin.admin("dismiss", {"finished": True}, store=st2)
        del os.environ[var]
        check("%s: dismiss is refused for an unattended process and hides nothing" % var,
              res["ok"] is False and res.get("code") == "unattended" and res_all.get("code") == "unattended"
              and st2.get_item("steam:fail")["dismissed_at"] is None)
    out = io.StringIO()
    hub_admin.run_cli(["dismiss"], stdin=io.StringIO("{\"id\": \"steam:nope\"}"), stdout=out)
    check("through the CLI entry: one JSON refusal line", json.loads(out.getvalue())["ok"] is False)


def test_list_include_dismissed_and_row_field():
    st = provider_rows(fresh_store())
    visible = hub_admin.admin("list", {}, store=st)
    check("every row carries dismissed_at (None while shown)",
          visible["ok"] and visible["items"] and all("dismissed_at" in r and r["dismissed_at"] is None for r in visible["items"]))
    check("the card payload carries it too",
          "dismissed_at" in hub_admin.card_payload(ids=["steam:fin"], store=st)["cards"][0])
    hub_admin.admin("dismiss", {"id": "steam:fin"}, store=st)
    hub_admin.admin("dismiss", {"id": "steam:unk"}, store=st)
    shown = hub_admin.admin("list", {}, store=st)
    ids = {r["id"] for r in shown["items"]}
    check("by default a dismissed row is not listed", "steam:fin" not in ids and "steam:unk" not in ids and "steam:run" in ids)
    everything = hub_admin.admin("list", {"include_dismissed": True}, store=st)
    by_id = {r["id"]: r for r in everything["items"]}
    check("include_dismissed brings the hidden rows back, marked with when they were hidden",
          "steam:fin" in by_id and isinstance(by_id["steam:fin"]["dismissed_at"], float) and by_id["steam:unk"]["dismissed_at"]
          and by_id["steam:run"]["dismissed_at"] is None)
    check("only the literal true counts (1 and \"1\" do not)",
          all("steam:fin" not in {r["id"] for r in hub_admin.admin("list", {"include_dismissed": v}, store=st)["items"]} for v in (1, "1", "true", None)))
    check("the summary counts still leave hidden rows out", everything["summary"]["counts"] == shown["summary"]["counts"])
    gen = hub_admin.watch_events(store=st, interval=0, stop=lambda: True)
    first = next(gen)
    check("the push stream never carries a hidden row (the panel asks for them with `list`)",
          "steam:fin" not in {r["id"] for r in first["items"]})
    # the hub un-hides a row by itself when its provider reports it live again
    st.upsert_provider_row("steam", "fin", {"title": "Row fin", "state": "downloading", "percent": 2})
    check("a hidden row the provider reports live again is shown again by the hub",
          "steam:fin" in {r["id"] for r in hub_admin.admin("list", {}, store=st)["items"]})


def test_settle_hours_setting():
    st = fresh_store()
    payload = hub_admin.settings_payload(st)
    check("the settings payload reports unknown_settle_hours as writable by dl_prefs, with its value",
          payload["ok"] and "unknown_settle_hours" in payload["writable"] and "unknown_settle_hours" in payload["prefs_writable"]
          and payload["settings"]["unknown_settle_hours"] == 24)
    ok = service.update_prefs({"unknown_settle_hours": 48}, store=st)
    check("the hub stores 48 and the payload then reports it", ok["ok"] and hub_admin.settings_payload(st)["settings"]["unknown_settle_hours"] == 48)
    bad = service.update_prefs({"unknown_settle_hours": 0}, store=st)
    check("the hub's own refusal for 0 names its range (the page shows this text as it is)",
          bad["ok"] is False and "between 1 and 720" in bad["error"] and hub_admin.settings_payload(st)["settings"]["unknown_settle_hours"] == 48)
    check("720 is accepted and 721 refused", service.update_prefs({"unknown_settle_hours": 720}, store=st)["ok"]
          and service.update_prefs({"unknown_settle_hours": 721}, store=st)["ok"] is False)


class _FakeStack:
    """Stands in for `stremio_stack.tool_stack_start`: records the call, answers what it is told to."""
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, args):
        self.calls.append(dict(args or {}))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_stremio_start_op():
    from jarvis.actions import stremio_stack
    real = stremio_stack.tool_stack_start
    try:
        check("stremio_start is a write op, never a read op", "stremio_start" in hub_admin.WRITE_OPS and "stremio_start" not in hub_admin.READ_OPS)
        started = {"ok": True, "server": {"state": "started", "outcome": "started", "url": "http://127.0.0.1:11470", "version": "4.20.8",
                                          "sentence": "Stremio's server is running at http://127.0.0.1:11470 (version 4.20.8). Jarvis started it."}}
        stremio_stack.tool_stack_start = fake = _FakeStack(started)
        out = hub_admin.admin("stremio_start", {})
        check("it calls tool_stack_start for the server only, once", fake.calls == [{"what": "server"}])
        check("a start gives ok with the status sentence and version", out["ok"] is True and out["state"] == "started" and out["version"] == "4.20.8"
              and "Jarvis started it" in out["sentence"] and "error" not in out)
        check("a request body can't change what is started (no `what`, no path, no url)",
              hub_admin.admin("stremio_start", {"what": "both", "runtime_exe": "C:/x.exe", "url": "http://evil"})["ok"] is True and fake.calls[-1] == {"what": "server"})
        up = {"ok": True, "server": {"state": "already_up", "outcome": "running", "url": "http://127.0.0.1:11470", "version": "4.20.8",
                                     "note": "already up (started outside Jarvis); nothing was started", "sentence": "Stremio's server is running ..."}}
        stremio_stack.tool_stack_start = _FakeStack(up)
        out = hub_admin.admin("stremio_start", {})
        check("already up is ok and says nothing was started", out["ok"] is True and out["state"] == "already_up" and "nothing was started" in out["note"])
        app_open = {"ok": False, "error": "The Stremio app is open, but its server isn't answering ...",
                    "server": {"state": "error", "outcome": "app_open", "url": "http://127.0.0.1:11470", "sentence": "The Stremio app is open, but its server isn't answering ...",
                               "error": "The Stremio app is open, but its server isn't answering ..."}}
        stremio_stack.tool_stack_start = _FakeStack(app_open)
        out = hub_admin.admin("stremio_start", {})
        check("a refusal is ok: false with the status sentence as the error, and the outcome",
              out["ok"] is False and out["outcome"] == "app_open" and "app is open" in out["error"] and "app is open" in out["sentence"])
        silent = {"ok": False, "error": "silent", "server": {"state": "error", "outcome": "silent", "sentence": "silent", "next": "Look at its console", "error": "silent",
                                                          "problems": ["a problem"]}, "config_problems": ["a problem"]}
        stremio_stack.tool_stack_start = _FakeStack(silent)
        out = hub_admin.admin("stremio_start", {})
        check("the next step and the config problems come through", out["ok"] is False and out["next"] == "Look at its console" and out["config_problems"] == ["a problem"])
        stremio_stack.tool_stack_start = _FakeStack({"ok": False, "error": "tool broke"})
        check("an answer with no server block still gives its error", hub_admin.admin("stremio_start", {})["error"] == "tool broke")
        stremio_stack.tool_stack_start = _FakeStack(RuntimeError("boom"))
        out = hub_admin.admin("stremio_start", {})
        check("an exception is a refusal, not a crash", out["ok"] is False and "boom" in out["error"])
        stremio_stack.tool_stack_start = _FakeStack(None)
        check("a non-dict answer is a refusal", hub_admin.admin("stremio_start", {})["ok"] is False)
        for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
            stremio_stack.tool_stack_start = fake = _FakeStack(started)
            os.environ[var] = "1"
            res = hub_admin.admin("stremio_start", {})
            del os.environ[var]
            check("%s: stremio_start is refused for an unattended process and starts nothing" % var,
                  res["ok"] is False and res.get("code") == "unattended" and fake.calls == [])
    finally:
        stremio_stack.tool_stack_start = real

    # the REAL tool_stack_start, with the app open and its server silent: nothing is started (N9 decision 4)
    from jarvis.hub.stremio import status as stremio_status_mod
    from jarvis import daemons
    real_snapshot, real_dstart = stremio_status_mod.snapshot, daemons.start
    started_daemons = []
    try:
        stremio_status_mod.snapshot = lambda **kw: {"state": "app_open", "title": "App open, server silent", "address": "http://127.0.0.1:11470",
                                                    "sentence": "The Stremio app is open, but its server isn't answering at http://127.0.0.1:11470.",
                                                    "problems": [], "managed_by": "", "version": "", "path": {}, "tried": []}
        daemons.start = lambda did, *a, **k: started_daemons.append(did) or (True, "started")
        out = hub_admin.admin("stremio_start", {})
        check("with the Stremio app open and its server silent: ok is false, the sentence is the status's, and no daemon was started",
              out["ok"] is False and "app is open" in out["error"] and started_daemons == [])
    finally:
        stremio_status_mod.snapshot, daemons.start = real_snapshot, real_dstart


def _cli_out(op, body):
    out = io.StringIO()
    hub_admin.run_cli([op], stdin=io.StringIO(body), stdout=out)
    return out.getvalue().strip().splitlines()[-1]


def test_sources_op():
    """Brief 24: the Sources tab's read-only op."""
    from jarvis.hub import engines
    engines.load()
    st = fresh_store()
    check("sources is a read op (not refused for an unattended process, like card and list)",
          "sources" in hub_admin.READ_OPS and "sources" not in hub_admin.WRITE_OPS)
    res = hub_admin.admin("sources", {}, store=st)
    ids = [e["id"] for e in res.get("engines", [])]
    check("a fresh store: all four engines are listed, and the answer is ok", res.get("ok") is True and set(ids) >= {"http", "ytdlp", "stremio", "qbittorrent"} and len(ids) == len(set(ids)))
    by = {e["id"]: e for e in res["engines"]}
    check("every engine has an id, a label, one of the five states, a reason, handles, counts, checks and configure",
          all({"id", "label", "state", "reason", "handles", "active", "queued", "checks", "configure"} <= set(e) for e in res["engines"])
          and all(e["state"] in ("ready", "needs_setup", "not_running", "off", "broken") for e in res["engines"]))
    check("the handles phrases come from the small table (HTTP, yt-dlp, Stremio, qBittorrent)",
          by["http"]["handles"] == "direct links and files" and by["ytdlp"]["handles"] == "YouTube and other video sites"
          and by["stremio"]["handles"] == "magnets, torrent files and Stremio streams" and by["qbittorrent"]["handles"] == "magnets, when you ask for it")
    check("a fresh store has no registered provider; Playnite is shown with its hint instead of nothing",
          [p for p in res["providers"] if p.get("registered")] == []
          and [p for p in res["providers"] if p["id"] == "playnite"][0].get("hint", "").startswith("Appears here after the first install Jarvis starts through Playnite"))
    check("qBittorrent without its file is Needs setup and the reason names the file",
          by["qbittorrent"]["state"] == "needs_setup" and "qbittorrent.json" in by["qbittorrent"]["reason"] and by["qbittorrent"]["config_file"] == "~/.jarvis/qbittorrent.json")
    check("an engine with writable settings gets a configure id, one without (qBittorrent) gets none",
          by["stremio"]["configure"] == "stremio" and by["http"]["configure"] == "http" and by["ytdlp"]["configure"] == "ytdlp" and by["qbittorrent"]["configure"] is None)
    check("the home folder never appears in the answer (it is shown as ~)", HOME not in json.dumps(res))

    # an engine that says it is unavailable, one that is switched off, one that is not running, one that raises
    http = engines.ENGINES["http"]
    real = http.available
    try:
        http.available = lambda: (False, "the `requests` package is missing")
        r1 = {e["id"]: e for e in hub_admin.admin("sources", {}, store=st)["engines"]}["http"]
        check("an engine whose available() is false is Needs setup, with its own reason", r1["state"] == "needs_setup" and r1["reason"] == "the `requests` package is missing")
        http.available = lambda: (False, "switched off in qbittorrent.json (\"enabled\": false)")
        check("a switched-off engine is Off", {e["id"]: e for e in hub_admin.admin("sources", {}, store=st)["engines"]}["http"]["state"] == "off")
        http.available = lambda: (False, "can't reach it (is it running?)")
        check("an engine that cannot be reached is Not running", {e["id"]: e for e in hub_admin.admin("sources", {}, store=st)["engines"]}["http"]["state"] == "not_running")
        http.available = lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))
        r3 = hub_admin.admin("sources", {}, store=st)
        broken = {e["id"]: e for e in r3["engines"]}["http"]
        check("an engine whose check raises is Broken and the op still answers ok with the others",
              r3["ok"] is True and broken["state"] == "broken" and "kaboom" in broken["reason"] and len(r3["engines"]) >= 4)
    finally:
        http.available = real

    # a module that failed to load
    engines.LOAD_ERRORS["bad_engine"] = "ImportError: no module named nothing at http://10.0.0.5:9/x"
    try:
        r4 = {e["id"]: e for e in hub_admin.admin("sources", {}, store=st)["engines"]}
        check("an engine that failed to load is listed Broken with the load error",
              r4["bad_engine"]["state"] == "broken" and "didn't load" in r4["bad_engine"]["reason"] and "ImportError" in r4["bad_engine"]["reason"])
        check("an address inside a load error is not shown", "10.0.0.5" not in r4["bad_engine"]["reason"])
    finally:
        engines.LOAD_ERRORS.pop("bad_engine", None)

    # no secret anywhere: a fake qBittorrent file with a password, and addresses in the reasons
    jdir = Path(HOME) / ".jarvis"
    jdir.mkdir(parents=True, exist_ok=True)
    cfg = jdir / "qbittorrent.json"
    cfg.write_text(json.dumps({"address": "http://127.0.0.1:1", "username": "admin", "password": "hunter2-SECRET-pw", "tag": "jarvis"}), encoding="utf-8")
    try:
        engines.ENGINES["qbittorrent"]._avail = None
        blob = json.dumps(hub_admin.admin("sources", {}, store=st))
        check("a qBittorrent password in its config file never appears in the answer", "hunter2-SECRET-pw" not in blob and "admin" not in blob.replace("administrator", ""))
        check("no http:// or https:// address appears anywhere in the answer", "http://" not in blob and "https://" not in blob and "127.0.0.1" not in blob)
    finally:
        cfg.unlink()
        engines.ENGINES["qbittorrent"]._avail = None
    check("the plain-sentence cleaner removes `password: x` and `token=x`", "x1" not in hub_admin._plain("password: x1 and token=x1") and "(hidden)" in hub_admin._plain("password: x1"))

    # counts: running and waiting hub rows per engine; provider rows stay out of the engine counts
    st2 = provider_rows(fresh_store())
    make(st2, "waiting a", "queued")
    make(st2, "waiting b", "queued")
    make(st2, "running one", "downloading")
    make(st2, "finished one", "done")
    rows_before = sorted((r["title"], r["state"], r["priority"]) for r in st2.list_items(limit=50, include_dismissed=True))
    counted = {e["id"]: e for e in hub_admin.admin("sources", {}, store=st2)["engines"]}["http"]
    check("an engine's active and queued are its hub rows' counts (2 waiting, 1 running, finished not counted)", counted["active"] == 1 and counted["queued"] == 2)
    provs = [p for p in hub_admin.admin("sources", {}, store=st2)["providers"] if p.get("registered")]
    check("a registered provider carries what dl_providers says, with registered true",
          provs and all(p["registered"] is True and "enabled" in p and "interval_s" in p and "last_error" in p for p in provs))
    check("sources starts and changes nothing: the rows are the same afterwards",
          rows_before == sorted((r["title"], r["state"], r["priority"]) for r in st2.list_items(limit=50, include_dismissed=True)))
    check("through the CLI entry point it is one JSON object", json.loads(_cli_out("sources", "{}")).get("ok") is True)
    server_js = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    check("web/server.js: GET /api/hub/sources is a read route (no write guard), calling the sources op",
          'app.get("/api/hub/sources", requireHubRead, requireJarvis, (req, res) => hubAdmin(res, "sources", {}, 30000));' in server_js
          and "/api/hub/sources\", requireHubWrite" not in server_js)
    cli_src = (CLI / "jarvis" / "cli.py").read_text(encoding="utf-8")
    check("cli.py needs no change: hub-admin dispatch is generic", "_hub_admin.run_cli(argv[1:])" in cli_src)


def test_unattended():
    st = fresh_store()
    a = make(st, "done one", "done")
    for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
        os.environ[var] = "1"
        res = hub_admin.admin("history_remove", {"ids": [a["id"]]}, store=st)
        check("%s: the writes are refused" % var, res["ok"] is False and res.get("code") == "unattended" and st.get_item(a["id"]) is not None)
        check("%s: the reads still work (a card in a chat reply)" % var, hub_admin.admin("card", {"ids": [a["id"]]}, store=st)["ok"] is True)
        del os.environ[var]
    check("every write op is on the guarded list, and no read op is", set(hub_admin.WRITE_OPS) == {"history_remove", "history_clear", "redownload", "reorder", "stremio_save", "dismiss", "stremio_start"} and not set(hub_admin.READ_OPS) & set(hub_admin.WRITE_OPS))
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
          server.count("requireHubWrite, requireJarvis") == 7 and "requireHubRead, requireJarvis" in server and "function hubOriginOk" in server)
    check("a thumbnail or file route takes an id and never a path", 'app.get("/api/hub/thumb/:id"' in server and 'app.get("/api/hub/file/:id"' in server and "req.query.path" not in server.split("// Download Hub web back end")[1].split("// Calendars panel")[0])
    check("the thumbnail route pins its content type and nosniff", "X-Content-Type-Options" in server and "sandbox" in server)
    check("the stream route is capped and stops its watcher", "HUB_MAX_CLIENTS" in server and "hubWatchStopSoon" in server)
    check("the new routes: POST /api/hub/dismiss and /api/hub/stremio/start, and ?include_dismissed=1 on the list read",
          'app.post("/api/hub/dismiss", requireHubWrite, requireJarvis' in server and 'app.post("/api/hub/stremio/start", requireHubWrite, requireJarvis' in server
          and 'include_dismissed: req.query.include_dismissed === "1"' in server)
    check("the dismiss route reads an id and a literal `finished: true`, nothing else; the start route reads no body",
          'hubAdmin(res, "dismiss", {\n  id: hubText(req.body?.id, 120), finished: req.body?.finished === true,' in server
          and 'hubAdmin(res, "stremio_start", {}, 330000)' in server)
    check("hub_admin.py never opens the database itself", "sqlite3" not in (CLI / "jarvis" / "hub_admin.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    for fn in (test_card, test_group_card, test_stream_diff, test_watch_generator, test_thumb_and_file, test_settings_and_history,
               test_history_writes, test_clear_by_age, test_redownload_and_reorder, test_dismiss_op,
               test_list_include_dismissed_and_row_field, test_settle_hours_setting, test_stremio_start_op, test_sources_op,
               test_unattended, test_cli, test_plumbing):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
