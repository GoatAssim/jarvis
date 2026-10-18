"""Download Hub: a folder per file extension, a folder per download, notifications per download.

Covers: the `ext_folders` setting (validation, the dl_prefs-vs-dl_settings split for full paths);
where a file lands (own folder > extension folder > kind folder, longest extension first, a bad
entry never blocks a download); a full path on one download needing approval and being the only
way outside the library; changing a queued download's folder / channels / events; and the
Dispatcher honouring a download's own channels and events (kept apart from other messages,
working while the global switch is off, left out of the queue-empty tally).

Run: python tests/test_hub_dest_notify.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-dest-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.hub import notify, service, settings as hs  # noqa: E402
from jarvis.hub.engines import http as http_engine  # noqa: E402
from jarvis.hub.engines.base import EngineContext  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

PUBLIC = "http://93.184.216.34"
passed = failed = 0
_n = [0]


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


class Clock:
    def __init__(self, t=2_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def fresh_store(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / f"dest-{_n[0]}.db", clock=clock or time.time)


def add(store, name="a.zip", options=None):
    item, _ = store.add_item(engine="http", source_url=f"https://example.com/{name}", title=name,
                             options=options or {})
    return item


def finish(store, item, state="done", **fields):
    if store.get_item(item["id"])["state"] == "queued":
        with store._write() as conn:
            conn.execute("UPDATE items SET state='probing', attempts=attempts+1, started_at=COALESCE(started_at, ?) "
                         "WHERE id=?", (store.now(), item["id"]))
    if state == "done":
        fields.setdefault("dest_file", "/library/Files/" + item["title"])
    return store.transition(item["id"], state, **fields)


class Sink:
    """Stands in for notifier.notify and records every message, including a channel list when given."""

    def __init__(self):
        self.sent = []

    def __call__(self, title, message, *, level=None, failed=False, channels=None):
        self.sent.append({"title": title, "message": message, "level": level, "failed": failed, "channels": channels})
        return {"delivered": ["inbox"], "failed_channels": []}


# ------------------------------------------------------------------ the setting

def test_setting():
    check("ext_folders defaults to empty", hs.DEFAULTS["ext_folders"] == {})
    check("clean_ext adds the dot and lower-cases", hs.clean_ext("ISO") == ".iso" and hs.clean_ext(".PDF") == ".pdf")
    check("clean_ext keeps a compound extension", hs.clean_ext("tar.gz") == ".tar.gz")
    check("clean_ext refuses junk", all(hs.clean_ext(x) is None for x in ("", ".", "a/b", "..x", ".a b", "x" * 40)))

    clean, err = hs.validate_update({"ext_folders": {"ISO": "ISOs", ".pdf": "Docs/Work"}}, allowed=hs.PREFS_WRITABLE)
    check("relative extension folders are an everyday preference",
          clean == {"ext_folders": {".iso": "ISOs", ".pdf": "Docs/Work"}} and not err)
    _c, err = hs.validate_update({"ext_folders": {".iso": "/mnt/isos"}}, allowed=hs.PREFS_WRITABLE)
    check("a full path needs dl_settings", err and "dl_settings" in err[0])
    _c, err = hs.validate_update({"ext_folders": {".iso": "D:/ISOs"}}, allowed=hs.PREFS_WRITABLE)
    check("a drive path needs dl_settings too", bool(err))
    clean, err = hs.validate_update({"ext_folders": {".iso": "/mnt/isos"}})
    check("dl_settings accepts a full path", clean == {"ext_folders": {".iso": "/mnt/isos"}} and not err)
    check("'..' is refused in a relative folder", hs.validate_update({"ext_folders": {".iso": "../x"}}, allowed=hs.PREFS_WRITABLE)[1])
    check("'..' is refused in a full path", hs.validate_update({"ext_folders": {".iso": "/mnt/../etc"}})[1])
    check("a filesystem root is refused", hs.validate_update({"ext_folders": {".iso": "/"}})[1])
    check("a bad extension is refused", hs.validate_update({"ext_folders": {"a/b": "X"}}, allowed=hs.PREFS_WRITABLE)[1])
    check("an empty folder removes an extension",
          hs.validate_update({"ext_folders": {".iso": ""}}, allowed=hs.PREFS_WRITABLE)[0] == {"ext_folders": {}})
    check("not an object is refused", hs.validate_update({"ext_folders": "iso=ISOs"}, allowed=hs.PREFS_WRITABLE)[1])

    store = fresh_store()
    res = service.update_prefs({"ext_folders": {".iso": "ISOs"}}, store=store)
    check("dl_prefs saves it and the settings view shows it",
          res.get("ok") and res["settings"]["ext_folders"] == {".iso": "ISOs"})
    res = service.update_prefs({"ext_folders": {".pdf": "Docs"}}, store=store)
    check("writing the map replaces it (the panel sends the whole map)", res["settings"]["ext_folders"] == {".pdf": "Docs"})
    res = service.update_prefs({"ext_folders": {}}, store=store)
    check("an empty map clears them all", res["settings"]["ext_folders"] == {})
    res = service.update_settings({"ext_folders": {".iso": "/mnt/isos"}}, store=store)
    check("dl_settings saves a full path", res.get("ok") and res["settings"]["ext_folders"] == {".iso": "/mnt/isos"})


# ------------------------------------------------------------------ where a file goes

def test_resolution():
    root = Path(HOME) / "libres"
    base = hs.merged({"library_root": str(root)})

    d, src = hs.resolve_dest_dir(base, "movie.mkv", "video", {})
    check("no rules: the kind folder", d == root / "Video" and src == "kind")
    d, src = hs.resolve_dest_dir(base, "doc.pdf", "file", {})
    check("no rules: files go to Files", d == root / "Files")

    cfg = hs.merged({"library_root": str(root), "ext_folders": {".iso": "ISOs", ".mkv": "Films", ".gz": "Zips",
                                                                ".tar.gz": "Tarballs", ".pdf": "/mnt/docs-abs"}})
    d, src = hs.resolve_dest_dir(cfg, "linux.ISO", "file", {})
    check("an extension folder beats the kind folder (case-insensitive)", d == root / "ISOs" and src == "extension")
    d, _ = hs.resolve_dest_dir(cfg, "movie.mkv", "video", {})
    check("it beats the Video folder too", d == root / "Films")
    d, _ = hs.resolve_dest_dir(cfg, "pack.tar.gz", "file", {})
    check("the longer extension wins", d == root / "Tarballs")
    d, _ = hs.resolve_dest_dir(cfg, "single.gz", "file", {})
    check("a plain .gz still uses its own", d == root / "Zips")
    d, _ = hs.resolve_dest_dir(cfg, "other.txt", "file", {})
    check("an extension with no rule falls back to the kind folder", d == root / "Files")
    d, src = hs.resolve_dest_dir(cfg, "paper.pdf", "file", {})
    check("an extension folder can be a full path", d == Path("/mnt/docs-abs") and src == "extension")

    d, src = hs.resolve_dest_dir(cfg, "linux.iso", "file", {"subdir": "Mine"})
    check("a download's own sub-folder beats the extension folder", d == root / "Mine" and src == "download")
    d, src = hs.resolve_dest_dir(cfg, "linux.iso", "file", {"dest_abs": "/mnt/elsewhere"})
    check("a download's own full path beats everything", d == Path("/mnt/elsewhere") and src == "download")
    d, _ = hs.resolve_dest_dir(cfg, "linux.iso", "file", {"subdir": "../x"})
    check("a bad folder on the download itself is an error, not a guess", d is None)

    broken = hs.merged({"library_root": str(root)})
    broken["ext_folders"] = {".iso": "../escape"}
    d, src = hs.resolve_dest_dir(broken, "linux.iso", "file", {})
    check("a bad stored extension entry never blocks the download", d == root / "Files" and src == "kind")

    roots = hs.write_roots(cfg, {"dest_abs": "/mnt/elsewhere"})
    check("write_roots: library + full-path extension folders + the download's own",
          roots == [root, Path("/mnt/docs-abs"), Path("/mnt/elsewhere")])
    check("write_roots: just the library when nothing else is set", hs.write_roots(base) == [root])


def test_engine_end_to_end():
    server, base_url, state = srv.start()
    try:
        store = fresh_store()
        lib = Path(HOME) / "e2e-lib"
        extra = Path(HOME) / "e2e-abs"
        store.set_settings({"library_root": str(lib), "allow_private_hosts": ["127.0.0.1"],
                            "ext_folders": {".bin": "Blobs"}})

        def go(url, **options):
            state.reset()
            item = store.add_item(engine="http", source_url=url, title="", options=options)[0]
            claimed = store.claim_next({"default": 100}, 100)
            assert claimed and claimed["id"] == item["id"]
            ctx = EngineContext(store, claimed, store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
            outcome = http_engine.ENGINE.run(claimed, ctx)
            return store.get_item(item["id"]), outcome

        row, _o = go(base_url + "/file.bin?size=2000")
        check("a real download lands in its extension's folder",
              row.get("dest_dir") == str(lib / "Blobs") and (lib / "Blobs" / "file.bin").exists())
        row, _o = go(base_url + "/file.bin?size=2000", subdir="Mine")
        check("a download's own sub-folder wins over the extension's",
              row.get("dest_dir") == str(lib / "Mine") and (lib / "Mine" / "file.bin").exists())
        row, _o = go(base_url + "/file.bin?size=2000", dest_abs=str(extra))
        check("a download's own full path is used (outside the library)",
              row.get("dest_dir") == str(extra) and (extra / "file.bin").exists())
        store.set_settings({"ext_folders": {".iso": "ISOs"}})
        row, _o = go(base_url + "/norange.bin?size=2000")
        check("an extension with no rule still uses the kind folder", row.get("dest_dir") == str(lib / "Files"))
    finally:
        srv.stop(server)


# ------------------------------------------------------------------ service: options

def test_service_options():
    store = fresh_store()
    store.set_settings({"library_root": str(Path(HOME) / "svc-lib")})

    res = service.add_items([PUBLIC + "/a.zip"], dest="/mnt/anywhere", store=store)
    check("a full path is refused until approved", res["ok"] is False and "approved" in res["error"])
    res = service.add_items([PUBLIC + "/a.zip"], dest="/mnt/anywhere", allow_absolute_dest=True, store=store)
    check("with approval it is stored as dest_abs",
          res["ok"] and store.get_item(res["added"][0]["id"])["options"] == {"dest_abs": "/mnt/anywhere"})
    res = service.add_items([PUBLIC + "/a2.zip"], dest="/mnt/../etc", allow_absolute_dest=True, store=store)
    check("a full path with '..' is refused even when approved", res["ok"] is False)
    res = service.add_items([PUBLIC + "/a3.zip"], dest="Manuals", store=store)
    check("a sub-folder still works and is stored as subdir", store.get_item(res["added"][0]["id"])["options"] == {"subdir": "Manuals"})
    same = service.add_items([PUBLIC + "/a.zip"], dest="/mnt/other", allow_absolute_dest=True, store=store)
    check("the same link into a different full path is a separate download", not same["added"][0]["duplicate"])

    res = service.add_items([PUBLIC + "/b.zip"], notify_channels=["discord"], notify_events=["done", "failed"],
                            dest="Y", store=store)
    opts = store.get_item(res["added"][0]["id"])["options"]
    check("channels and events are stored on the download with its folder",
          opts == {"subdir": "Y", "notify_channels": ["discord"], "notify_events": ["done", "failed"]})
    res = service.add_items([PUBLIC + "/c.zip"], notify_channels="Discord, toast", store=store)
    check("a comma list works and the legacy 'toast' name means desktop",
          store.get_item(res["added"][0]["id"])["options"]["notify_channels"] == ["discord", "desktop"])
    res = service.add_items([PUBLIC + "/d.zip"], notify_channels=["carrier-pigeon"], store=store)
    check("an unknown channel is refused and names the real ones", res["ok"] is False and "discord" in res["error"])
    res = service.add_items([PUBLIC + "/e.zip"], notify_events=["lunch"], store=store)
    check("an unknown event is refused", res["ok"] is False and "started" in res["error"])
    res = service.add_items([PUBLIC + "/f.zip"], notify_channels="default", store=store)
    check("'default' leaves it unset", store.get_item(res["added"][0]["id"])["options"] == {})

    # change a queued download
    item = store.get_item(service.add_items([PUBLIC + "/g.zip"], store=store)["added"][0]["id"])
    res = service.update_prefs({"notify_channels": ["discord"], "notify_events": ["done"], "folder": "Games"},
                               id=item["id"], store=store)
    check("a queued download's channels, events and folder can be set",
          res["ok"] and res["options"] == {"notify_channels": ["discord"], "notify_events": ["done"], "subdir": "Games"})
    res = service.update_prefs({"folder": "/mnt/big"}, id=item["id"], store=store)
    check("a full path needs approval here too", res["ok"] is False and "approved" in res["error"])
    res = service.update_prefs({"folder": "/mnt/big"}, id=item["id"], allow_absolute_dest=True, store=store)
    check("approved: it replaces the sub-folder (never both)", res["options"].get("dest_abs") == "/mnt/big" and "subdir" not in res["options"])
    res = service.update_prefs({"folder": "default", "channels": "default"}, id=item["id"], store=store)
    check("'default' for the folder is a sub-folder named default, not a reset", res["ok"] is True)
    res = service.update_prefs({"folder": ""}, id=item["id"], store=store)
    check("an empty folder goes back to the default rules", res["ok"] and "subdir" not in res["options"] and "dest_abs" not in res["options"])
    res = service.update_prefs({"notify_channels": []}, id=item["id"], store=store)
    check("an empty channel list goes back to the person's settings", res["ok"] and "notify_channels" not in res["options"])

    started = add(store, "started.zip")
    store.transition(started["id"], "downloading", dest_file="/x/started.zip", dest_dir="/x")
    res = service.update_prefs({"folder": "Late"}, id=started["id"], store=store)
    check("the folder can't change once the file is being written", res["ok"] is False and "started" in res["error"])
    res = service.update_prefs({"notify_channels": ["discord"]}, id=started["id"], store=store)
    check("but its notifications still can", res["ok"] is True)

    service.update_prefs({"notify_channels": ["discord"], "folder": "Games"}, id=item["id"], store=store)
    listed = service.list_items(search="g.zip", store=store)
    rows = [r for r in (listed.get("items") or listed.get("rows") or []) if r.get("id") == item["id"]]
    prefs = (rows[0].get("prefs") if rows else None) or {}
    check("the row's prefs report its folder and notification choices",
          prefs.get("subdir") == "Games" and prefs.get("notify_channels") == ["discord"])


def test_open_roots():
    store = fresh_store()
    lib = Path(HOME) / "open-lib"
    outside = Path(HOME) / "open-outside"
    outside.mkdir(parents=True, exist_ok=True)
    f = outside / "x.bin"
    f.write_bytes(b"1")
    store.set_settings({"library_root": str(lib)})
    plain = add(store, "x.bin")
    finish(store, plain, dest_file=str(f))
    launched = []
    old = service.LAUNCH
    service.LAUNCH = lambda target, action: launched.append((target, action)) or True
    try:
        res = service.open_item(plain["id"], "reveal", store=store)
        check("a file outside the library can't be opened when nobody chose that folder", res["ok"] is False)
        own = add(store, "y.bin", options={"dest_abs": str(outside)})
        finish(store, own, dest_file=str(f))
        res = service.open_item(own["id"], "reveal", store=store)
        check("a download that was sent there on purpose can be opened", res["ok"] is True and launched)
    finally:
        service.LAUNCH = old


# ------------------------------------------------------------------ notifications per download

def dispatcher(settings=None, clock=None):
    store = fresh_store(clock)
    if settings:
        store.set_settings(settings)
    clock = clock or Clock()
    sink = Sink()
    return store, sink, notify.Dispatcher(store, deliver=sink, clock=clock), clock


def test_notifications():
    clock = Clock()
    store, sink, disp, clock = dispatcher(clock=clock)
    disp.since()
    clock.advance(1)
    plain = add(store, "plain.zip")
    mine = add(store, "mine.zip", options={"notify_channels": ["discord"]})
    finish(store, plain)
    finish(store, mine)
    disp.scan(store.get_settings())
    clock.advance(30)
    disp.scan(store.get_settings())
    by_channel = {tuple(m["channels"] or ()): m for m in sink.sent if m["title"].startswith("Download finished")}
    check("two finishes at once become two messages, one per destination", len(by_channel) == 2)
    check("the download with its own channels goes only to them", ("discord",) in by_channel and "mine.zip" in by_channel[("discord",)]["message"])
    check("and its name is not in the other message", () in by_channel and "mine.zip" not in by_channel[()]["message"])
    check("the other one is delivered the usual way (no channels given)", "plain.zip" in by_channel[()]["message"])

    # per-download event list, global 'started' is off
    clock2 = Clock()
    store, sink, disp, clock2 = dispatcher(clock=clock2)
    disp.since()
    clock2.advance(1)
    only_failed = add(store, "of.zip", options={"notify_events": ["failed"]})
    finish(store, only_failed)
    disp.scan(store.get_settings())
    clock2.advance(30)
    disp.scan(store.get_settings())
    check("a download that only wants failures is not announced when it finishes",
          not [m for m in sink.sent if "of.zip" in m["message"]])
    wants_start = add(store, "ws.zip", options={"notify_events": ["started", "done"]})
    finish(store, wants_start, state="downloading")
    disp.scan(store.get_settings())
    clock2.advance(30)
    disp.scan(store.get_settings())
    check("'started' fires for a download that asked for it even though it is off globally",
          [m for m in sink.sent if m["title"].startswith("Download started") and "ws.zip" in m["message"]])

    # global switch off
    clock3 = Clock()
    store, sink, disp, clock3 = dispatcher({"notify.enabled": False}, clock=clock3)
    disp.since()
    clock3.advance(1)
    quiet = add(store, "quiet.zip")
    loud = add(store, "loud.zip", options={"notify_channels": ["discord"]})
    finish(store, quiet)
    finish(store, loud)
    disp.scan(store.get_settings())
    clock3.advance(30)
    disp.scan(store.get_settings())
    check("with notifications off globally, a download with its own channels still speaks",
          [m for m in sink.sent if "loud.zip" in m["message"] and m["channels"] == ["discord"]])
    check("and nothing else does, including no 'all finished' message",
          not [m for m in sink.sent if "quiet.zip" in m["message"] or m["title"] == "All downloads finished"])

    # queue-empty tally ignores a download announced elsewhere
    clock4 = Clock()
    store, sink, disp, clock4 = dispatcher(clock=clock4)
    disp.since()
    clock4.advance(1)
    solo = add(store, "solo.zip", options={"notify_channels": ["discord"]})
    finish(store, solo)
    disp.scan(store.get_settings())
    clock4.advance(30)
    disp.scan(store.get_settings())
    check("a download going to Discord only doesn't trigger the 'all downloads finished' message on other channels",
          not [m for m in sink.sent if m["title"] == "All downloads finished"])

    # explicit 'off' still wins
    clock5 = Clock()
    store, sink, disp, clock5 = dispatcher(clock=clock5)
    disp.since()
    clock5.advance(1)
    muted = add(store, "muted.zip", options={"notify": "off", "notify_channels": ["discord"]})
    finish(store, muted)
    disp.scan(store.get_settings())
    clock5.advance(30)
    disp.scan(store.get_settings())
    check("notify=off silences it even when channels are set", not [m for m in sink.sent if "muted.zip" in m["message"]])


def main():
    for fn in (test_setting, test_resolution, test_engine_end_to_end, test_service_options, test_open_roots,
               test_notifications):
        print(f"\n== {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a crash is a failure with a name
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} ran to the end ({exc})", False)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
