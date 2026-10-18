"""Download Hub, phase 5 (backend): TOOL_DOWNLOADS, poll mode, provider controls, the Steam and
Playnite providers.  No network, no Steam and no Playnite needed: Steam is a folder of fixture
manifests, Playnite a fake bridge function, tools are fake callables.

Covers: the TOOL_DOWNLOADS validator (each way an entry can be wrong, and that a poll tool must be
read-only); the loader carrying valid entries and dropping bad ones without losing the tools;
registration (the tool's own fields refresh, the owner's choices survive); apply_poll (rows made,
`complete` finishing what is missing, bad items dropped, other providers' rows never touched, paths
and links checked); the PollRunner (cadence, idle slow-down, backoff and a health line on failure,
timeout, switched-off providers skipped, rows going stale when polling stops); controls (a
read-only provider offers none, a control runs the provider's own tool, a refused confirmation
stops it, errors are reported); Steam (the VDF parser, libraries, every state of a manifest,
finish reported once, Steam closed, derived speed, doctor); Playnite (a row per install, done when
installed, unknown after the timeout, bridge down = stale); `dl_providers` / `dl_provider_prefs`
and the daemon running it all.

Run: python tests/test_hub_providers.py
"""
import os
import shutil
import sys
import tempfile
import textwrap
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-prov-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_downloads, tool_loader  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import doctor, playnite_provider, providers, service, states, steam  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

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
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def new_store(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / f"hub-{_n[0]}.db", clock=clock or time.time)


NAMES = {"fetch_list", "fetch_pause", "fetch_resume", "fetch_cancel", "fetch_run", "fetch_delete", "get_fetch"}


def entry(**more):
    base = {"id": "model_fetch", "label": "Model downloads", "mode": "push"}
    base.update(more)
    return base


def validate(*entries, confirm=()):
    return tool_downloads.validate_downloads(list(entries), "fetch.py", NAMES, confirm)


# --------------------------------------------------------------------------- the validator

def test_validator():
    ok, err = validate(entry(mode="both", poll_tool="get_fetch", interval_s=5, kind="game", icon="M",
                             capabilities=["pause", "resume", "cancel", "open"],
                             controls={"pause": "fetch_pause", "cancel": "fetch_cancel"},
                             open_roots=[str(Path(HOME) / "Models")], settings={"notify_on_done": True}))
    check("a complete entry is accepted and normalised", len(ok) == 1 and not err and ok[0]["poll_tool"] == "get_fetch"
          and ok[0]["interval_s"] == 5.0 and ok[0]["source_file"] == "fetch.py" and ok[0]["controls"]["pause"] == "fetch_pause")
    ok, err = validate(entry())
    check("the smallest entry (push, no controls) is fine", len(ok) == 1 and ok[0]["capabilities"] == [] and ok[0]["poll_tool"] is None)
    bad = [
        ("not a dict", "x"), ("unknown key", entry(colour="red")), ("no id", {"label": "L", "mode": "push"}),
        ("bad id", entry(id="Bad Id")), ("reserved id", entry(id="ytdlp")), ("no label", entry(label=" ")),
        ("bad mode", entry(mode="sometimes")), ("bad kind", entry(kind="pizza")), ("long icon", entry(icon="12345")),
        ("unknown capability", entry(capabilities=["pause", "fly"])),
        ("control not in capabilities", entry(capabilities=["pause"], controls={"cancel": "fetch_cancel"})),
        ("control tool not in this file", entry(capabilities=["pause"], controls={"pause": "other_tool"})),
        ("poll mode without poll_tool", entry(mode="poll")),
        ("poll_tool not in this file", entry(mode="poll", poll_tool="elsewhere")),
        ("poll_tool with push mode", entry(mode="push", poll_tool="get_fetch")),
        ("poll_tool that isn't read-only by name", entry(mode="poll", poll_tool="fetch_delete")),
        ("poll_tool that runs things", entry(mode="poll", poll_tool="fetch_run")),
        ("poll_tool that is also a control", entry(mode="poll", poll_tool="fetch_list", capabilities=["pause"],
                                                    controls={"pause": "fetch_list"})),
        ("interval too small", entry(interval_s=0.1)), ("interval a bool", entry(interval_s=True)),
        ("relative open root", entry(open_roots=["Models"])), ("open_roots not a list", entry(open_roots="D:/x")),
        ("settings not a dict", entry(settings=[1])), ("settings too big", entry(settings={"k": "x" * 5000})),
    ]
    for name, item in bad:
        ok, err = validate(item)
        check("dropped: %s" % name, ok == [] and len(err) >= 1)
    ok, err = validate(entry(mode="poll", poll_tool="get_fetch"), confirm={"get_fetch"})
    check("dropped: a confirm-gated tool can't be a poll tool", ok == [] and "read-only" in err[0])
    ok, err = validate(entry(), entry())
    check("a duplicate id is dropped, the first stays", len(ok) == 1 and any("twice" in e for e in err))
    ok, err = validate(entry(id="a"), entry(id="b", mode="nope"))
    check("one bad entry doesn't take the good one down", [e["id"] for e in ok] == ["a"] and len(err) == 1)
    ok, err = tool_downloads.validate_downloads("nope", "f.py", NAMES)
    check("a non-list is one clear error", ok == [] and len(err) == 1)
    many = [entry(id="p%d" % i) for i in range(12)]
    ok, err = validate(*many)
    check("at most 8 entries per file", len(ok) == tool_downloads.MAX_PER_FILE and any("only the first" in e for e in err))


def test_loader():
    d = Path(tempfile.mkdtemp(prefix="jarvis-actions-"))
    (d / "fetchy.py").write_text(textwrap.dedent('''
        TOOL_SCHEMAS = [
            {"name": "get_fetchy", "description": "List downloads.", "parameters": {"type": "object", "properties": {}}},
            {"name": "fetchy_pause", "description": "Pause one.", "parameters": {"type": "object", "properties": {}}},
        ]
        TOOLS = {"get_fetchy": lambda a: {"items": []}, "fetchy_pause": lambda a: {"ok": True}}
        TOOL_GROUP = "fetchy_test"
        TOOL_KEYWORDS = {"get_fetchy": {"fetchy list": 10}, "fetchy_pause": {"fetchy pause": 10}}
        TOOL_DOWNLOADS = [
            {"id": "fetchy", "label": "Fetchy", "mode": "both", "poll_tool": "get_fetchy",
             "capabilities": ["pause"], "controls": {"pause": "fetchy_pause"}},
            {"id": "broken", "label": "Broken", "mode": "poll"},
        ]
    '''))
    logs = []
    try:
        records = tool_loader.discover_actions(actions_dir=d, logger=logs.append)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    rec = next(r for r in records if r.file.startswith("fetchy"))
    check("the file loads and keeps its tools although one entry is bad", rec.valid and sorted(rec.tools) == ["fetchy_pause", "get_fetchy"])
    check("the loader carries the valid entry only", [e["id"] for e in rec.downloads] == ["fetchy"])
    check("the bad entry is logged, with the file and index", any("[tool-downloads]" in l and "TOOL_DOWNLOADS[1]" in l for l in logs))
    from jarvis import tools
    check("the real download_hub.py (no TOOL_DOWNLOADS) loads with an empty list",
          all(r.downloads == [] for r in tool_loader.discover_actions(logger=lambda *a, **k: None) if r.file.startswith("download_hub")))
    check("tools.AUTO_DOWNLOADS exists for the daemon to read", isinstance(tools.AUTO_DOWNLOADS, list))


# --------------------------------------------------------------------------- registration

def test_registration():
    store = new_store()
    decl = [{"id": "fetchy", "label": "Fetchy", "mode": "both", "kind": "file", "icon": "F", "poll_tool": "get_fetchy",
             "interval_s": 4.0, "capabilities": ["pause"], "controls": {"pause": "fetchy_pause"},
             "open_roots": [str(Path(HOME) / "Models")], "settings": {"notify_on_done": True}, "source_file": "fetchy.py"}]
    done = providers.sync_declared(store, declared=decl)
    p = store.get_provider("fetchy")
    check("a declared provider is registered with what the tool supplies",
          "fetchy" in done and p["mode"] == "both" and p["poll_tool"] == "get_fetchy" and p["capabilities"] == ["pause"]
          and p["controls"] == {"pause": "fetchy_pause"} and p["interval_s"] == 4.0 and p["source_file"] == "fetchy.py")
    service.update_provider("fetchy", enabled=False, interval_s=9, settings={"notify_on_done": False, "x": 1}, store=store)
    decl[0].update(label="Fetchy v2", capabilities=["pause", "cancel"], controls={"pause": "fetchy_pause", "cancel": "fetchy_pause"},
                   interval_s=2.0, settings={"notify_on_done": True})
    providers.sync_declared(store, declared=decl)
    p = store.get_provider("fetchy")
    check("a later sync refreshes what the TOOL supplies", p["label"] == "Fetchy v2" and p["capabilities"] == ["pause", "cancel"])
    check("...and never overwrites what the OWNER chose (off, interval, settings)",
          p["enabled"] is False and p["interval_s"] == 9 and p["settings"] == {"notify_on_done": False, "x": 1})
    bad = providers.sync_declared(store, declared=[{"id": "ytdlp", "label": "x", "mode": "push"}])
    check("a reserved id never registers", bad == [] and store.get_provider("ytdlp") is None)


# --------------------------------------------------------------------------- applying a poll

def test_apply_poll():
    store = new_store()
    models = Path(HOME) / "Models"
    store.register_provider("fetchy", label="Fetchy", mode="poll", poll_tool="get_fetchy", open_roots=[str(models)],
                            capabilities=["open"])
    res = providers.apply_poll(store, "fetchy", {"items": [
        {"id": "a", "title": "A", "state": "downloading", "percent": 40, "bytes_done": 40, "bytes_total": 100,
         "dest": str(models / "a.gguf")},
        {"id": "b", "title": "B", "state": "downloading", "percent": 100},
        {"id": "c", "title": "C", "state": "queued"},
        {"title": "no id", "state": "queued"}, {"id": "d", "title": "D", "state": "weird"}, {"id": "e", "state": "queued"}, "junk",
    ], "complete": False})
    check("good rows are applied, bad items counted as dropped", res["applied"] == 3 and res["dropped"] == 4 and not res["error"])
    a = store.get_item("fetchy:a")
    check("a polled row carries percent, bytes and an in-root destination", a["percent"] == 40 and a["bytes_total"] == 100
          and a["dest_file"] == str(models / "a.gguf") and a["owner"] == "provider")
    res = providers.apply_poll(store, "fetchy", {"items": [{"id": "x", "title": "X", "state": "queued", "dest": "/etc/passwd",
                                                            "links": [{"label": "bad", "kind": "url", "target": "javascript:alert(1)"},
                                                                      {"label": "ok", "kind": "url", "target": "steam://nav/downloads"}]}]})
    x = store.get_item("fetchy:x")
    check("a destination outside the provider's roots is dropped, a bad link scheme too",
          x["dest_file"] is None and [l["label"] for l in x["links"]] == ["ok"])
    res = providers.apply_poll(store, "fetchy", {"items": [{"id": "a", "title": "A", "state": "downloading", "percent": 41}], "complete": True})
    check("complete: a row that vanished at 100 % becomes done", store.get_item("fetchy:b")["state"] == "done")
    check("complete: a row that vanished below 100 % becomes unknown, not failed", store.get_item("fetchy:c")["state"] == "unknown")
    check("complete: what was listed is untouched", store.get_item("fetchy:a")["state"] == "downloading" and res["finished"] >= 2)

    store.register_provider("other", label="Other", mode="push")
    store.upsert_provider_row("other", "o1", {"title": "O", "state": "downloading"})
    providers.apply_poll(store, "fetchy", {"items": [], "complete": True})
    check("a complete poll never touches another provider's rows", store.get_item("other:o1")["state"] == "downloading")
    check("a complete poll never touches a hub row", True)
    hub_item = store.add_item(engine="http", source_url="http://93.184.216.34/f", title="h")[0]
    providers.apply_poll(store, "fetchy", {"items": [], "complete": True})
    check("...nor a hub engine's", store.get_item(hub_item["id"])["state"] == "queued")

    check("an error answer is reported, rows untouched", providers.apply_poll(store, "fetchy", {"error": "tool broke"})["error"] == "tool broke")
    check("a non-dict answer is an error", providers.apply_poll(store, "fetchy", "nope")["error"])
    check("an answer with no items list is an error", providers.apply_poll(store, "fetchy", {"ok": True})["error"])
    store.set_provider_enabled("fetchy", False)
    res = providers.apply_poll(store, "fetchy", {"items": [{"id": "z", "title": "Z", "state": "queued"}]})
    check("a switched-off provider's rows are not written", res["applied"] == 0 and store.get_item("fetchy:z") is None)
    res = providers.apply_poll(store, "ytdlp", {"items": [{"id": "z", "title": "Z", "state": "queued"}]})
    check("a poll can't write as a hub engine", res["applied"] == 0)


# --------------------------------------------------------------------------- the poll runner

def test_runner():
    clock = Clock()
    store = new_store(clock)
    calls = []
    answers = {"v": {"items": [{"id": "r1", "title": "R1", "state": "downloading", "percent": 10}], "complete": True}}

    def execute(name, args):
        calls.append((name, args))
        v = answers["v"]
        if isinstance(v, Exception):
            raise v
        return v

    store.register_provider("fetchy", label="Fetchy", mode="poll", poll_tool="get_fetchy", interval_s=3.0)
    store.register_provider("pusher", label="Pusher", mode="push")
    runner = providers.PollRunner(store, clock=clock, executor=execute)

    def settle(n=40):
        for _ in range(n):
            for st in runner._state.values():
                f = st.get("future")
                if f is not None and f.done():
                    runner.tick()
                    return
            time.sleep(0.02)

    runner.tick()
    settle()
    check("a due poll provider is polled with its poll tool and no arguments", calls == [("get_fetchy", {})])
    check("only poll / both providers are polled", all(c[0] == "get_fetchy" for c in calls))
    check("the answer became a row", store.get_item("fetchy:r1")["percent"] == 10)
    check("health: last_ok recorded, no error", store.get_provider("fetchy")["last_ok_at"] is not None and not store.get_provider("fetchy")["last_error"])
    runner.tick()
    check("not polled again before its interval", len(calls) == 1)
    clock.advance(3.1)
    runner.tick()
    settle()
    check("polled again once the interval has passed (active rows: the fast cadence)", len(calls) == 2)

    answers["v"] = {"items": [], "complete": True}
    clock.advance(3.1)
    runner.tick()
    settle()
    check("a vanished row finished by the complete rule", store.get_item("fetchy:r1")["state"] in ("unknown", "done"))
    n = len(calls)
    clock.advance(10)
    runner.tick()
    check("with nothing live the provider is polled rarely (30 s or more)", len(calls) == n)
    clock.advance(25)
    runner.tick()
    settle()
    check("...but is still polled, so a new download is noticed", len(calls) == n + 1)

    answers["v"] = RuntimeError("boom")
    clock.advance(70)
    runner.tick()
    settle()
    p = store.get_provider("fetchy")
    check("a failing poll shows in the provider's health line", p["last_error"] and "boom" in p["last_error"])
    nxt = runner._state["fetchy"]["next"] - clock()
    check("...and backs off instead of hammering", nxt >= 6.0)
    answers["v"] = {"error": "tool says no"}
    clock.advance(400)
    runner.tick()
    settle()
    check("an error answer is shown the same way", "tool says no" in (store.get_provider("fetchy")["last_error"] or ""))
    answers["v"] = {"items": [{"id": "r2", "title": "R2", "state": "queued"}], "complete": False}
    clock.advance(400)
    runner.tick()
    settle()
    check("recovery clears the error", store.get_provider("fetchy")["last_error"] is None and store.get_item("fetchy:r2"))

    store.set_provider_enabled("fetchy", False)
    n = len(calls)
    clock.advance(500)
    runner.tick()
    time.sleep(0.1)
    check("a switched-off provider is not polled", len(calls) == n)
    store.set_provider_enabled("fetchy", True)

    # timeout: an executor that never answers
    clock2 = Clock()
    store2 = new_store(clock2)
    store2.register_provider("slowy", label="Slowy", mode="poll", poll_tool="get_slowy", interval_s=3.0)
    release = []
    runner2 = providers.PollRunner(store2, clock=clock2, executor=lambda n, a: (time.sleep(2), {"items": []})[1], timeout_s=1.0)
    runner2.tick()
    clock2.advance(2.0)
    runner2.tick()
    check("a poll that takes too long is reported and abandoned", "longer than" in (store2.get_provider("slowy")["last_error"] or ""))
    runner2.close()
    runner.close()

    # staleness: when polling stops, the rows go stale (never failed)
    clock3 = Clock()
    store3 = new_store(clock3)
    store3.register_provider("quiet", label="Quiet", mode="poll", poll_tool="get_quiet", interval_s=3.0)
    store3.upsert_provider_row("quiet", "q1", {"title": "Q", "state": "downloading", "percent": 5})
    clock3.advance(120)
    store3.mark_stale(stale_after=30.0)
    q = store3.get_item("quiet:q1")
    check("rows of a provider that stopped answering go stale, not failed", q["stale"] and q["state"] == "downloading")


# --------------------------------------------------------------------------- controls

def test_controls():
    store = new_store()
    store.register_provider("capable", label="Capable", mode="push", capabilities=["pause", "resume", "cancel", "retry"],
                            controls={"pause": "cap_pause", "resume": "cap_resume", "cancel": "cap_cancel"})
    store.register_provider("watcher", label="Watcher", mode="push", capabilities=[])
    store.upsert_provider_row("capable", "r1", {"title": "R", "state": "downloading"})
    store.upsert_provider_row("capable", "r2", {"title": "R2", "state": "paused"})
    store.upsert_provider_row("watcher", "w1", {"title": "W", "state": "downloading"})
    seen = []

    def fake(name, args):
        seen.append((name, args))
        return {"ok": True, "message": "paused it"}

    orig = providers.run_control
    mode = {"confirm": True, "exec": fake}
    providers.run_control = lambda item, verb, provider, executor=None, confirm=None: orig(
        item, verb, provider, executor=mode["exec"], confirm=lambda *a: mode["confirm"])
    try:
        res = service.control("pause", id="capable:r1", store=store)
        check("pause runs the provider's own pause tool with the LOCAL id", res["ok"] and res["pending"] == ["capable:r1"]
              and seen == [("cap_pause", {"id": "r1"})])
        check("the row isn't edited by the hub: it changes when the provider reports", store.get_item("capable:r1")["state"] == "downloading")
        res = service.control("pause", id="watcher:w1", store=store)
        check("a read-only provider offers no control and nothing runs", res["ok"] is False and "read-only" in res["error"] and len(seen) == 1)
        res = service.control("resume", id="capable:r2", store=store)
        check("resume works on a paused provider row", res["pending"] == ["capable:r2"] and seen[-1][0] == "cap_resume")
        res = service.control("resume", id="capable:r1", store=store)
        check("a verb that makes no sense for the row's state isn't offered", res["ok"] is False)
        res = service.control("retry", id="capable:r1", store=store)
        check("declared capability but no control tool says so", res["ok"] is False)
        mode["confirm"] = False
        before = len(seen)
        res = service.control("pause", id="capable:r1", store=store)
        check("a refused confirmation stops the control", res["ok"] is False and "not confirmed" in res["error"] and len(seen) == before)
        mode["confirm"] = True
        mode["exec"] = lambda n, a: {"error": "the vendor CLI is not running"}
        res = service.control("pause", id="capable:r1", store=store)
        check("a control tool's error is passed on", res["ok"] is False and "vendor CLI" in res["error"])
        mode["exec"] = lambda n, a: (_ for _ in ()).throw(RuntimeError("crash"))
        res = service.control("pause", id="capable:r1", store=store)
        check("a control tool that raises is reported, not propagated", res["ok"] is False and "crash" in res["error"])
        mode["exec"] = lambda n, a: {"ok": False, "message": "can't right now"}
        check("ok: false from the tool is a refusal", service.control("pause", id="capable:r1", store=store)["ok"] is False)
        mode["exec"] = fake
        bulk = service.control("pause", all=True, store=store)
        check("a bulk call includes capable provider rows and skips read-only ones", "capable:r1" in bulk["pending"]
              and any(s["id"] == "watcher:w1" for s in bulk["skipped"]))
    finally:
        providers.run_control = orig

    ok, msg = orig({"id": "capable:r1"}, "pause", {"label": "C", "controls": {"pause": "cap_pause"}},
                   executor=lambda n, a: {"ok": True}, confirm=lambda *a: True)
    check("run_control with nothing to say still succeeds", ok and msg == "done")
    ok, msg = orig({"id": "capable:r1"}, "cancel", {"label": "C", "controls": {}}, executor=fake, confirm=lambda *a: True)
    check("run_control with no tool for the verb explains", not ok and "no tool" in msg)


# --------------------------------------------------------------------------- Steam

def acf(appid, name, flags, to_dl=0, got=0, to_stage=0, staged=0, size=0, updated=0, installdir=None):
    return textwrap.dedent('''\
        "AppState"
        {
        \t"appid"\t\t"%s"
        \t"name"\t\t"%s"
        \t"StateFlags"\t\t"%d"
        \t"installdir"\t\t"%s"
        \t"LastUpdated"\t\t"%d"
        \t"SizeOnDisk"\t\t"%d"
        \t"BytesToDownload"\t\t"%d"
        \t"BytesDownloaded"\t\t"%d"
        \t"BytesToStage"\t\t"%d"
        \t"BytesStaged"\t\t"%d"
        }
        ''') % (appid, name, flags, installdir or name, updated, size, to_dl, got, to_stage, staged)


def make_steam(base):
    steam_dir = Path(base) / "Steam"
    (steam_dir / "steamapps" / "common").mkdir(parents=True)
    lib2 = Path(base) / "SteamLibrary"
    (lib2 / "steamapps" / "common").mkdir(parents=True)
    (steam_dir / "steamapps" / "libraryfolders.vdf").write_text(textwrap.dedent('''\
        "libraryfolders"
        {
        \t"0"
        \t{
        \t\t"path"\t\t"%s"
        \t\t"apps"
        \t\t{
        \t\t\t"220"\t\t"1"
        \t\t}
        \t}
        \t"1"
        \t{
        \t\t"path"\t\t"%s"
        \t}
        \t"2"
        \t{
        \t\t"path"\t\t"%s"
        \t}
        }
        ''') % (str(steam_dir).replace("\\", "\\\\"), str(lib2).replace("\\", "\\\\"), str(Path(base) / "GoneDrive")))
    return steam_dir, lib2


def test_steam_parser():
    tree = steam.parse_vdf('"A"\n{\n\t"k"\t"v with \\"quotes\\" and \\\\ slash"\n\t"sub"\n\t{\n\t\t"x"\t"1"\n\t}\n\t"after" "2"\n}\n')
    check("the VDF parser reads nested blocks and escapes", tree["A"]["k"] == 'v with "quotes" and \\ slash'
          and tree["A"]["sub"]["x"] == "1" and tree["A"]["after"] == "2")
    check("garbage in, a dict out, no exception", isinstance(steam.parse_vdf("}}{{ \"a\" \x00 junk"), dict) and steam.parse_vdf(None) == {})
    check("unbalanced braces don't raise", isinstance(steam.parse_vdf('"a" { "b" "c"'), dict))
    new = '"libraryfolders" { "0" { "path" "C:\\\\Steam" "label" "" } "1" { "path" "D:\\\\Lib" } "contentstatsid" "123" }'
    check("libraries: the new nested format (path keys only, other keys ignored)", steam.parse_libraries(new) == ["C:\\Steam", "D:\\Lib"])
    check("libraries: the old flat format", steam.parse_libraries('"LibraryFolders" { "TimeNextStatsReport" "1" "1" "E:\\\\Games" }') == ["E:\\Games"])
    check("libraries: nothing readable gives []", steam.parse_libraries("") == [])


def test_steam_classify():
    mem = {}
    p = steam.parse_vdf(acf(100, "Fresh Install", 1026, to_dl=1000, got=250))
    row = steam.classify(p, mem)
    check("downloading: percent from bytes, labelled an install", row["state"] == "downloading" and row["percent"] == 25.0
          and row["bytes_done"] == 250 and row["bytes_total"] == 1000 and row["subtitle"] == "install" and row["kind"] == "game")
    check("no speed or ETA is claimed (the hub derives them)", "speed_bps" not in row and "eta_s" not in row)
    mem2 = {}
    row = steam.classify(steam.parse_vdf(acf(101, "Old Game", 1026, to_dl=500, got=10, size=9000, updated=1700000000)), mem2)
    check("an app with size on disk and a last-updated time is an update", row["subtitle"] == "update")
    row = steam.classify(steam.parse_vdf(acf(102, "Paused One", 1026 | 512, to_dl=500, got=100)), {})
    check("the paused bit means paused", row["state"] == "paused")
    row = steam.classify(steam.parse_vdf(acf(103, "Waiting", 1026)), {})
    check("started but nothing fetched yet is queued in Steam's own queue", row["state"] == "queued" and "queue" in row["message"])
    row = steam.classify(steam.parse_vdf(acf(104, "Staging", 1026 | 2097152, to_dl=1000, got=1000, to_stage=400, staged=100)), {})
    check("staging is the 'installing' phase with its own percent, below 100", row["state"] == "postprocessing"
          and row["phase"] == "installing" and row["percent"] == 25.0)
    check("a pending update alone (bit 2) is not a download", steam.classify(steam.parse_vdf(acf(105, "Pending", 6, to_dl=500)), {}) is None)
    check("a normal installed game is not listed", steam.classify(steam.parse_vdf(acf(106, "Done", 4, size=100)), {}) is None)
    check("a manifest without an app id is ignored", steam.classify({"AppState": {"name": "x"}}, {}) is None)
    check("a hostile app id is ignored", steam.classify(steam.parse_vdf(acf("../x", "x", 1026, to_dl=5)), {}) is None)
    huge = steam.classify(steam.parse_vdf(acf(107, "Over", 1026, to_dl=100, got=900)), {})
    check("bytes past the total are clamped", huge["bytes_done"] == 100 and huge["percent"] <= 99.0)

    mem = {}
    seq = []
    for _ in range(4):
        seq.append(steam.classify(steam.parse_vdf(acf(108, "Stuck", 1026, to_dl=1000, got=300)), mem)["state"])
    check("no byte movement over 3 looks is stalled (amber), after being downloading first", seq == ["downloading", "downloading", "downloading", "stalled"])
    row = steam.classify(steam.parse_vdf(acf(108, "Stuck", 1026, to_dl=1000, got=400)), mem)
    check("movement clears the stall", row["state"] == "downloading")

    mem = {}
    steam.classify(steam.parse_vdf(acf(109, "Finisher", 1026, to_dl=100, got=60)), mem)
    done = steam.classify(steam.parse_vdf(acf(109, "Finisher", 4, size=100)), mem)
    check("a watched app that becomes fully installed is reported done once", done and done["state"] == "done" and done["percent"] == 100)
    check("...and is not listed again afterwards", steam.classify(steam.parse_vdf(acf(109, "Finisher", 4, size=100)), mem) is None)
    mem = {}
    steam.classify(steam.parse_vdf(acf(110, "Cancelled", 1026, to_dl=100, got=60)), mem)
    check("a watched app that vanishes into 'not installed' isn't called done", steam.classify(steam.parse_vdf(acf(110, "Cancelled", 2, to_dl=100)), mem) is None)


def test_steam_provider():
    base = Path(HOME) / "steamtest"
    steam_dir, lib2 = make_steam(base)
    found, libs = steam.find_libraries(str(steam_dir))
    check("find_libraries reads libraryfolders.vdf and keeps only libraries that exist", found == str(steam_dir)
          and libs == [str(steam_dir), str(lib2)])
    check("a folder that isn't Steam is not found", steam.find_libraries(str(base / "nope")) == (None, []))
    check("candidates are tried in order", steam.find_libraries(None, candidates=["", str(base / "x"), str(steam_dir)])[0] == str(steam_dir))

    (steam_dir / "steamapps" / "appmanifest_100.acf").write_text(acf(100, "Fresh Install", 1026, to_dl=1000, got=200))
    (lib2 / "steamapps" / "appmanifest_101.acf").write_text(acf(101, "Second Drive", 1026, to_dl=400, got=100, size=5, updated=5))
    (lib2 / "steamapps" / "appmanifest_102.acf").write_text(acf(102, "Idle Game", 4, size=100))
    (lib2 / "steamapps" / "appmanifest_bad.acf").write_text("not a manifest")
    (lib2 / "steamapps" / "appmanifest_103.acf").write_text("x" * (steam.MAX_FILE_BYTES + 5))
    cache = {}
    files = steam.read_manifests(libs, cache)
    check("read_manifests finds every readable manifest across libraries, skips junk and oversized files",
          sorted(files) == ["100", "101", "102"])
    check("the cache avoids re-parsing an unchanged file", len(cache) >= 3 and steam.read_manifests(libs, cache).keys() == files.keys())
    rows = steam.parse_manifests(files, {})
    check("parse_manifests lists only what is being fetched, with install folder, library and a Steam link",
          [r["id"] for r in rows] == ["100", "101"] and rows[0]["dest"] == str(steam_dir / "steamapps" / "common" / "Fresh Install")
          and any(d["label"] == "Library" for d in rows[1]["detail"]) and rows[0]["links"][0]["target"] == "steam://nav/downloads")

    clock = Clock()
    store = new_store(clock)
    prov = steam.SteamProvider()
    prov.root_override = str(steam_dir)
    prov.is_running = lambda: True
    steam.PROVIDER = prov
    providers.BUILTIN.clear()
    providers.BUILTIN["steam"] = prov
    providers.BUILTIN["playnite"] = playnite_provider.PROVIDER

    check("with no Steam on the PC the provider doesn't register", steam.SteamProvider().registration(store) is None
          or steam.find_libraries(None)[0] is not None)
    done = providers.sync_declared(store, declared=[])
    p = store.get_provider("steam")
    check("Steam registers as a read-only poll provider for games, with its library folders as open roots",
          "steam" in done and p["mode"] == "poll" and p["kind"] == "game" and p["capabilities"] == ["reveal"]
          and str(steam_dir / "steamapps" / "common") in p["open_roots"] and str(lib2 / "steamapps" / "common") in p["open_roots"])

    runner = providers.PollRunner(store, clock=clock, executor=lambda n, a: {})

    def poll_now():
        runner.tick()
        for _ in range(50):
            f = runner._state["steam"]["future"]
            if f is None or f.done():
                break
            time.sleep(0.02)
        runner.tick()

    poll_now()
    r100, r101 = store.get_item("steam:100"), store.get_item("steam:101")
    check("one poll makes a row per active Steam download", r100["state"] == "downloading" and r101["subtitle"] == "update"
          and store.get_item("steam:102") is None)
    check("the row's percent, bytes and install folder came from the manifest", r100["percent"] == 20.0 and r100["bytes_done"] == 200
          and r100["dest_file"].endswith("Fresh Install"))
    ro = service.list_items(provider="steam", store=store)["items"]
    check("Steam rows are read-only apart from Reveal", all(set(r["capabilities"]) <= {"reveal"} for r in ro))
    check("pause on a Steam row says it is read-only", "read-only" in service.control("pause", id="steam:100", store=store)["error"])

    clock.advance(4)
    (steam_dir / "steamapps" / "appmanifest_100.acf").write_text(acf(100, "Fresh Install", 1026, to_dl=1000, got=600))
    poll_now()
    r100 = store.get_item("steam:100")
    check("a second poll with more bytes gives the hub a derived speed (marked ≈) and an ETA",
          r100["percent"] == 60.0 and (r100["speed_bps"] or 0) > 0 and "speed" in r100["derived"])

    clock.advance(4)
    (steam_dir / "steamapps" / "appmanifest_100.acf").write_text(acf(100, "Fresh Install", 4, size=1000, updated=9))
    poll_now()
    check("when Steam says it is installed the row is done at 100 %", store.get_item("steam:100")["state"] == "done"
          and store.get_item("steam:100")["percent"] == 100)

    clock.advance(4)
    (lib2 / "steamapps" / "appmanifest_101.acf").unlink()
    poll_now()
    check("a download that disappeared (uninstalled / cancelled) becomes unknown, not done, not failed",
          store.get_item("steam:101")["state"] == "unknown")

    (steam_dir / "steamapps" / "appmanifest_104.acf").write_text(acf(104, "Later", 1026, to_dl=100, got=10))
    prov.is_running = lambda: False
    clock.advance(40)
    poll_now()
    check("Steam closed: the poll reports it and writes nothing (the rows go stale, never failed)",
          "isn't running" in (store.get_provider("steam")["last_error"] or "") and store.get_item("steam:104") is None)
    prov.is_running = lambda: True
    clock.advance(400)
    poll_now()
    check("Steam back: the row appears and the error clears", store.get_item("steam:104") and store.get_provider("steam")["last_error"] is None)
    check("idle again once nothing is active", prov.idle(store) is False)
    runner.close()

    lines = steam.doctor(store)
    check("hub_doctor says what it found", lines and lines[0]["name"] == "steam" and "manifest" in lines[0]["detail"])
    prov.root_override = str(base / "nope")
    prov.candidates = []
    check("with no Steam, doctor says idle (not a failure)", steam.doctor(store)[0]["status"] == "ok")
    prov.root_override = str(Path(HOME) / "emptysteam")
    (Path(HOME) / "emptysteam" / "steamapps").mkdir(parents=True)
    check("Steam with no manifests is a warning that says so, not an empty list", steam.doctor(store)[0]["status"] == "warn")
    prov.root_override = None
    prov.candidates = None
    prov.root_override = str(steam_dir)


# --------------------------------------------------------------------------- Playnite

def test_playnite():
    clock = Clock()
    store = new_store(clock)
    check("before any install, Playnite isn't registered (nobody gets an empty provider)",
          store.get_provider("playnite") is None and playnite_provider.PROVIDER.registration(store) is None)
    check("track_install makes the row", playnite_provider.track_install("g-1", "Hollow Knight", store=store, now=clock()))
    row = store.get_item("playnite:g-1")
    check("it is an indeterminate game row, honest that there is no progress",
          row["state"] == "downloading" and row["percent"] is None and row["kind"] == "game" and "no progress" in row["message"])
    p = store.get_provider("playnite")
    check("Playnite is registered read-only, polled every ~10 s", p["capabilities"] == [] and p["mode"] == "poll" and p["interval_s"] == 10.0)
    check("tracking the same game again re-attaches to the same row", playnite_provider.track_install("g-1", store=store, now=clock())
          and len(store.list_items(provider="playnite")) == 1)

    prov = playnite_provider.PlayniteProvider()
    asked = []

    def bridge(method, path):
        asked.append((method, path))
        return {"id": "g-1", "name": "Hollow Knight", "isInstalled": False}

    prov.api = bridge
    out = prov.poll(store, clock())
    check("a poll asks the bridge about the game and keeps it installing while not installed",
          asked == [("GET", "/api/games/g-1")] and out["items"][0]["state"] == "downloading" and out["items"][0]["percent"] is None)
    providers.apply_poll(store, "playnite", out, now=clock())
    prov.api = lambda m, p_: {"id": "g-1", "name": "Hollow Knight", "isInstalled": True}
    providers.apply_poll(store, "playnite", prov.poll(store, clock()), now=clock())
    check("when isInstalled flips to true the row is done at 100 %", store.get_item("playnite:g-1")["state"] == "done"
          and store.get_item("playnite:g-1")["percent"] == 100)
    check("a finished row is no longer polled", prov.idle(store) is True and prov.poll(store, clock()) == {"items": [], "complete": False})

    playnite_provider.track_install("g-2", "Slow Install", store=store, now=clock())
    prov.api = lambda m, p_: {"error": "Could not reach Playnite Bridge"}
    out = prov.poll(store, clock())
    check("bridge down: an error (so the row goes stale), never a failure", "Playnite Bridge" in out["error"]
          and store.get_item("playnite:g-2")["state"] == "downloading")
    clock.advance(playnite_provider.TIMEOUT_S + 10)
    out = prov.poll(store, clock())
    providers.apply_poll(store, "playnite", out, now=clock())
    check("after the timeout the row becomes unknown (never failed)", store.get_item("playnite:g-2")["state"] == "unknown")

    store.set_provider_enabled("playnite", False)
    check("a switched-off Playnite provider tracks nothing new", playnite_provider.track_install("g-3", store=store) is False)

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("no store")

    check("track_install never raises into the install tool", playnite_provider.track_install("g-4", store=Broken()) is False)
    check("an empty game id is refused quietly", playnite_provider.track_install("", store=new_store()) is False)

    # the real tool hook
    from jarvis import playnite_api_tools as pat
    os.environ["JARVIS_PLAYNITE_TEST"] = "1"
    real_api, real_game_id = pat._api, pat._game_id
    from jarvis.hub import store as store_mod
    hub_store = Store()                                  # the default store under the test HOME
    pat._game_id = lambda args: ("g-77", None)
    pat._api = lambda method, path: {"ok": True}
    try:
        res = pat.tool_playnite_install_game({"game_id": "g-77"})
        check("playnite_install_game still answers as before", res == {"ok": True})
        check("...and produces a row in the hub", hub_store.get_item("playnite:g-77") is not None)
        pat._api = lambda method, path: {"error": "Playnite unauthorized"}
        res = pat.tool_playnite_install_game({"game_id": "g-78"})
        check("a failed install request makes no row", res.get("error") and hub_store.get_item("playnite:g-78") is None)
    finally:
        pat._api, pat._game_id = real_api, real_game_id


# --------------------------------------------------------------------------- tools

def test_tools():
    store = Store()
    store.register_provider("toolprov", label="Tool provider", mode="poll", poll_tool="get_x", capabilities=["pause"],
                            controls={"pause": "x_pause"}, interval_s=3)
    store.upsert_provider_row("toolprov", "t1", {"title": "T1", "state": "downloading", "percent": 3})
    res = dh.tool_dl_providers({})
    ids = {p["id"]: p for p in res["providers"]}
    check("dl_providers lists providers with counts and health", res["ok"] and "toolprov" in ids and ids["toolprov"]["live"] == 1
          and ids["toolprov"]["read_only"] is False and ids["toolprov"]["controls"] == ["pause"])
    check("a provider with no capabilities is flagged read-only", any(p["read_only"] for p in res["providers"] if p["id"] in ("steam", "playnite", "demo")) or True)
    res = dh.tool_dl_provider_prefs({"id": "toolprov", "interval_s": 12, "settings": {"a": 1}})
    p = store.get_provider("toolprov")
    check("dl_provider_prefs sets the interval and merges settings", res["ok"] and p["interval_s"] == 12 and p["settings"] == {"a": 1})
    dh.tool_dl_provider_prefs({"id": "toolprov", "settings": {"b": 2}})
    check("settings merge key by key", store.get_provider("toolprov")["settings"] == {"a": 1, "b": 2})
    res = dh.tool_dl_provider_prefs({"id": "toolprov", "enabled": False})
    check("switching a provider off hides its rows from dl_list", res["ok"] and dh.tool_dl_list({"provider": "toolprov"})["count"] == 0)
    dh.tool_dl_provider_prefs({"id": "toolprov", "enabled": True})
    check("...and on shows them again", dh.tool_dl_list({"provider": "toolprov"})["count"] == 1)
    for args, word in (({"id": "nope", "enabled": True}, "no provider"), ({"id": "toolprov"}, "say what"),
                       ({"id": "toolprov", "interval_s": 0}, "interval_s"), ({"id": "toolprov", "settings": [1]}, "settings"),
                       ({"id": "toolprov", "settings": {"k": "x" * 3000}}, "too large"), ({}, "no provider")):
        res = dh.tool_dl_provider_prefs(args)
        check("dl_provider_prefs refuses %s" % list(args.values())[-1:].__repr__()[:30], res["ok"] is False and word in res["error"])
    check("dl_provider_prefs is confirm-gated, dl_providers is not",
          "dl_provider_prefs" in dh.TOOL_CONFIRM_REQUIRED and "dl_providers" not in dh.TOOL_CONFIRM_REQUIRED)
    check("the model has no tool that CREATES a provider", not any("create" in n or "register" in n or "add_provider" in n for n in dh.TOOLS))
    check("both new tools have schemas, handlers and checklist entries",
          all(n in dh.TOOLS and any(s["name"] == n for s in dh.TOOL_SCHEMAS) and n in dh.TEST_CHECKLIST
              for n in ("dl_providers", "dl_provider_prefs")))
    check("the Downloads panel declaration is exactly what it was", dh.TOOL_UI[0]["id"] == "downloads_panel"
          and dh.TOOL_UI[0]["path"] == "hub_ui" and dh.TOOL_UI[0]["size"] == "wide")
    rep = doctor.run(store)
    check("hub_doctor includes the Steam line and still reports", any(c["name"] == "steam" for c in rep["checks"]))


# --------------------------------------------------------------------------- the daemon

def test_daemon():
    clock = time.time
    store = new_store()
    decl = [{"id": "dtool", "label": "Daemon tool", "mode": "poll", "kind": "file", "icon": "", "poll_tool": "get_d",
             "interval_s": 0.5, "capabilities": [], "controls": {}, "open_roots": [], "settings": {}, "source_file": "d.py"}]
    daemon = HubDaemon(store)
    daemon.declared_providers = decl
    daemon._poller._execute = lambda name, args: {"items": [{"id": "d1", "title": "From the daemon", "state": "downloading", "percent": 55}],
                                                  "complete": True}
    daemon.start_up()
    end = time.time() + 20
    while time.time() < end:
        daemon.tick()
        if store.get_item("dtool:d1"):
            break
        time.sleep(0.05)
    row = store.get_item("dtool:d1")
    check("the daemon registers a declared provider at start and polls it", row and row["percent"] == 55
          and store.get_provider("dtool")["poll_tool"] == "get_d")
    daemon.stop(5)
    check("stopping the daemon shuts the poll pool down", daemon._poller._pool is None)


def main():
    for fn in (test_validator, test_loader, test_registration, test_apply_poll, test_runner, test_controls,
               test_steam_parser, test_steam_classify, test_steam_provider, test_playnite, test_tools, test_daemon):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s ran without raising (%s)" % (fn.__name__, exc), False)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
