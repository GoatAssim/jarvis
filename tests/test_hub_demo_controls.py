"""Download Hub, brief 13 part A (N12): the demo provider's pause / resume / cancel.

No network and no daemon: the demo is a pure function of time, so a fake clock drives it.

Covers: the demo declares the three verbs and a control tool for each, and those tools are real
(discovered by the loader); the rows carry the buttons their state allows (and are no longer
read-only); each verb changes the row (pause holds the bar still, resume carries on from the same
place, cancel stays cancelled until the demo is started again); a verb the demo did not declare is
refused with a plain reason, and so is a verb that makes no sense for the row (already paused, not
paused, already done / failed / cancelled, demo not running, unknown row); a control can't reach
another provider's row (namespace isolation), both in `demo.control` and through the service's
provider-control path; and the whole thing end to end through the real tools
(`service.control` -> `tools.execute_tool` -> `hub_demo_*` -> the row).

Run: python tests/test_hub_demo_controls.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-democtl-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tools  # noqa: E402
from jarvis.actions import hub_demo_controls  # noqa: E402
from jarvis.hub import demo, service, states  # noqa: E402
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
    def __init__(self, t=5_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def fresh_store(clock):
    _n[0] += 1
    return Store(Path(HOME) / f"hub-{_n[0]}.db", clock=clock)


def running(clock=None):
    """A store with the demo started (rows written) and the provider registered."""
    clock = clock or Clock()
    store = fresh_store(clock)
    check("the demo starts", demo.start(store)["ok"])
    return clock, store


def run_for(clock, store, seconds):
    for _ in range(int(seconds)):
        clock.advance(1)
        demo.tick(store, clock())


def caps_of(store, clock, local_id):
    item = store.get_item("demo:%s" % local_id)
    return service.present_item(item, {p["id"]: p for p in store.list_providers()}, clock())["capabilities"]


# ---------------------------------------------------------------- what the demo declares

def test_declared():
    clock, store = running()
    provider = store.get_provider("demo")
    check("the demo declares pause, resume and cancel", provider["capabilities"] == ["pause", "resume", "cancel"])
    check("each verb has a control tool", provider["controls"] == {"pause": "hub_demo_pause", "resume": "hub_demo_resume", "cancel": "hub_demo_cancel"})
    check("the control tools are the ones in demo.CONTROLS", provider["controls"] == demo.CONTROLS)
    # the file also holds the fake update source's two tools (the Updates rework; tests/test_hub_updates_apply.py covers them)
    update_tools = {"check_demo_updates", "apply_demo_update"}
    check("those tools exist in the tool file", set(hub_demo_controls.TOOLS) - update_tools == set(demo.CONTROLS.values())
          and {s["name"] for s in hub_demo_controls.TOOL_SCHEMAS} - update_tools == set(demo.CONTROLS.values())
          and update_tools <= set(hub_demo_controls.TOOLS))
    check("and the loader really registered them (not 'unknown tool')",
          all("unknown" not in str(tools.execute_tool(name, {"id": "x"}).get("error", "")).lower() for name in demo.CONTROLS.values()))
    check("none of them asks for confirmation (a demo row has nothing real to lose)",
          not __import__("jarvis.tool_safety", fromlist=["x"]).requires_confirmation("hub_demo_cancel"))
    row = service.present_item(store.get_item("demo:climb"), {p["id"]: p for p in store.list_providers()}, clock())
    check("a demo row is no longer read-only", row["read_only"] is False)
    check("a running row offers Pause and Cancel, not Resume", caps_of(store, clock, "climb") == ["pause", "cancel"])
    check("a stalled row offers Pause and Cancel", caps_of(store, clock, "stalled") == ["pause", "cancel"])


# ---------------------------------------------------------------- rows_at (pure)

def test_rows_at():
    check("rows_at with no controls is the plain demo", demo.rows_at(30) == demo.rows_at(30, None) == demo.rows_at(30, {}))
    paused = demo.rows_at(30, {"climb": {"paused_since": 10.0}})["climb"]
    check("a paused row holds still at the point it was paused", paused["state"] == "paused"
          and paused["bytes_done"] == demo.rows_at(10)["climb"]["bytes_done"])
    check("a paused row keeps no speed or eta", "speed_bps" not in paused and "eta_s" not in paused)
    check("the same pause later still shows the same bar", demo.rows_at(50, {"climb": {"paused_since": 10.0}})["climb"]["bytes_done"] == paused["bytes_done"])
    resumed = demo.rows_at(40, {"climb": {"paused_since": None, "paused_total": 20.0}})["climb"]
    check("a resumed row carries on from where it stopped (its own clock lost the paused time)",
          resumed["state"] == "downloading" and resumed["bytes_done"] == demo.rows_at(20)["climb"]["bytes_done"])
    cancelled = demo.rows_at(50, {"failing": {"cancelled": True, "cancel_t": 5.0}})["failing"]
    check("a cancelled row is cancelled and stays so", cancelled["state"] == "cancelled" and "error" not in cancelled)
    check("other rows are not touched by one row's control", demo.rows_at(30, {"climb": {"paused_since": 10.0}})["stalled"] == demo.rows_at(30)["stalled"])
    check("an unknown id in the control block is ignored", demo.rows_at(30, {"nope": {"cancelled": True}}) == demo.rows_at(30))
    silent_paused = demo.rows_at(60, {"silent": {"paused_since": 12.0}})["silent"]
    check("a silent row that is paused or cancelled still reports (it has something to say now)",
          silent_paused is not None and silent_paused["state"] == "paused")


# ---------------------------------------------------------------- each verb changes the row

def test_verbs():
    clock, store = running()
    run_for(clock, store, 10)

    out = demo.control(store, "pause", "climb")
    check("pause is accepted", out["ok"] and out["message"] == "paused" and out["id"] == "demo:climb")
    check("the row is paused at once (no waiting for the next tick)", store.get_item("demo:climb")["state"] == "paused")
    held = store.get_item("demo:climb")["bytes_done"]
    check("a paused row now offers Resume and Cancel", caps_of(store, clock, "climb") == ["resume", "cancel"])
    run_for(clock, store, 20)
    check("it stays paused and the bar does not move", store.get_item("demo:climb")["state"] == "paused"
          and store.get_item("demo:climb")["bytes_done"] == held)
    check("the other rows kept going", store.get_item("demo:failing")["state"] == "failed" and store.get_item("demo:game")["state"] == "downloading")

    out = demo.control(store, "resume", "climb")
    check("resume is accepted", out["ok"] and out["message"] == "resumed")
    check("the row is downloading again", store.get_item("demo:climb")["state"] == "downloading")
    check("and starts from the same place", store.get_item("demo:climb")["bytes_done"] == held)
    run_for(clock, store, 10)
    check("it then climbs on", store.get_item("demo:climb")["bytes_done"] > held)
    check("a second pause works too", demo.control(store, "pause", "climb")["ok"] and store.get_item("demo:climb")["state"] == "paused")

    out = demo.control(store, "cancel", "indeterminate")
    check("cancel is accepted", out["ok"] and out["message"] == "cancelled")
    check("the row is cancelled", store.get_item("demo:indeterminate")["state"] == "cancelled")
    run_for(clock, store, 5)
    check("and stays cancelled after more ticks", store.get_item("demo:indeterminate")["state"] == "cancelled")
    check("a cancelled row offers nothing", caps_of(store, clock, "indeterminate") == [])

    out = demo.control(store, "cancel", "climb")
    check("a paused row can be cancelled", out["ok"] and store.get_item("demo:climb")["state"] == "cancelled")
    run_for(clock, store, 3)
    check("it stays cancelled (the pause is gone)", store.get_item("demo:climb")["state"] == "cancelled")

    # the stalled and silent rows are controllable too
    check("the stalled row can be paused", demo.control(store, "pause", "stalled")["ok"] and store.get_item("demo:stalled")["state"] == "paused")
    check("the game row can be cancelled", demo.control(store, "cancel", "game")["ok"] and store.get_item("demo:game")["state"] == "cancelled")

    demo.start(store)
    check("starting the demo again brings every row back", all(r["state"] not in ("paused", "cancelled") or r["id"] in ("demo:failing",)
                                                               for r in store.list_items(provider="demo", limit=50))
          and store.get_item("demo:indeterminate")["state"] == "downloading")
    run_for(clock, store, 2)
    check("and the old controls are gone", store.get_item("demo:climb")["state"] == "downloading")
    check("stop still cancels what is unfinished", demo.stop(store)["cancelled"] >= 3)


# ---------------------------------------------------------------- refusals say why

def test_refusals():
    clock, store = running()
    run_for(clock, store, 5)

    out = demo.control(store, "retry", "climb")
    check("a verb the demo did not declare is refused in plain words", out["ok"] is False and "doesn't offer retry" in out["error"])
    check("so is an empty verb", demo.control(store, "", "climb")["ok"] is False and demo.control(store, None, "climb")["ok"] is False)
    check("and a made-up one", "doesn't offer" in demo.control(store, "explode", "climb")["error"])
    check("a refused verb changes nothing", store.get_item("demo:climb")["state"] == "downloading")

    check("resume on a row that is not paused says so", demo.control(store, "resume", "climb")["error"] == "not paused")
    check("pausing twice says already paused", demo.control(store, "pause", "climb")["ok"]
          and demo.control(store, "pause", "climb")["error"] == "already paused")
    demo.control(store, "cancel", "climb")
    for verb in ("pause", "resume", "cancel"):
        check("%s on a cancelled row says already cancelled" % verb, demo.control(store, verb, "climb")["error"] == "already cancelled")

    run_for(clock, store, 20)
    check("pausing a failed row says already failed", demo.control(store, "pause", "failing")["error"] == "already failed")
    check("cancelling a failed row says already failed", demo.control(store, "cancel", "failing")["error"] == "already failed")
    fresh_clock, fresh = running()
    run_for(fresh_clock, fresh, 70)
    check("a finished row says already done", demo.control(fresh, "pause", "climb")["error"] == "already done"
          and demo.control(fresh, "cancel", "climb")["error"] == "already done")

    check("an unknown row id is refused", "no demo download called nope" in demo.control(store, "pause", "nope")["error"])
    check("an empty id is refused", demo.control(store, "pause", "")["ok"] is False and demo.control(store, "pause", None)["ok"] is False)

    demo.stop(store)
    out = demo.control(store, "pause", "game")
    check("with the demo stopped, a control says so", out["ok"] is False and "isn't running" in out["error"])
    off = fresh_store(Clock())
    check("with the demo never started, the same", "isn't running" in demo.control(off, "pause", "climb")["error"])
    check("the refusals never raise and always carry a reason",
          all(isinstance(demo.control(off, v, i).get("error"), str) for v in ("pause", "x", None) for i in ("climb", "a:b", None)))


# ---------------------------------------------------------------- namespace isolation

def test_isolation():
    clock, store = running()
    run_for(clock, store, 3)
    # another provider with its own rows, one of which has the SAME local id as a demo row
    store.register_provider("other", label="Other tool", mode="push", capabilities=["pause", "cancel"],
                            controls={"pause": "other_pause", "cancel": "other_cancel"})
    store.upsert_provider_row("other", "climb", {"title": "Other download", "state": "downloading", "percent": 10}, now=clock())
    store.upsert_provider_row("other", "r1", {"title": "Another", "state": "downloading", "percent": 20}, now=clock())
    store.register_provider("ro", label="Read-only tool", mode="push", capabilities=[], controls={})
    store.upsert_provider_row("ro", "x", {"title": "Reported only", "state": "downloading"}, now=clock())
    before = {i: store.get_item(i)["state"] for i in ("other:climb", "other:r1", "ro:x")}

    for target in ("other:r1", "other:climb", "ro:x", "demo:other:r1", "steam:1", "hub:abc"):
        out = demo.control(store, "cancel", target)
        check("demo.control refuses %s" % target, out["ok"] is False and "not a demo download" in out["error"])
    out = demo.control(store, "cancel", "r1")
    check("a bare local id that is only another provider's is not a demo row either", out["ok"] is False and "no demo download called r1" in out["error"])
    check("none of the other providers' rows changed", {i: store.get_item(i)["state"] for i in before} == before)

    out = demo.control(store, "pause", "climb")
    check("the demo's own 'climb' is paused", out["ok"] and store.get_item("demo:climb")["state"] == "paused")
    check("another provider's row with the same local id is untouched", store.get_item("other:climb")["state"] == "downloading")

    # through the service's provider-control path, with a spy in place of the tool runner
    calls = []

    def spy(name, args):
        calls.append((name, dict(args)))
        return {"ok": True, "message": "done"}

    providers = {p["id"]: p for p in store.list_providers()}
    res = service.control_provider_row(store.get_item("demo:stalled"), "pause", providers["demo"], executor=spy)
    check("a demo row runs the demo's own tool with its own local id", res[0] == "pending" and calls == [("hub_demo_pause", {"id": "stalled"})])
    calls.clear()
    res = service.control_provider_row(store.get_item("other:climb"), "pause", providers["other"], executor=spy)
    check("another provider's row runs that provider's tool, never the demo's", res[0] == "pending" and calls == [("other_pause", {"id": "climb"})])
    calls.clear()
    res = service.control_provider_row(store.get_item("other:climb"), "resume", providers["other"], executor=spy)
    check("a verb that provider did not declare for this state is refused in plain words, not as read-only", res[0] == "skipped" and "can't resume a download that is" in res[1] and "read-only" not in res[1] and not calls)
    res = service.control_provider_row(store.get_item("ro:x"), "pause", providers["ro"], executor=spy)
    check("a read-only provider's row is refused and nothing runs", res[0] == "skipped" and "read-only" in res[1] and not calls)
    res = service.control_provider_row(store.get_item("ro:x"), "cancel", providers["demo"], executor=spy)
    check("even handing the demo's provider to a read-only row only runs the demo tool with that row's id (the tool then refuses it)",
          res[0] == "pending" and calls == [("hub_demo_cancel", {"id": "x"})]
          and demo.control(store, "cancel", "x")["ok"] is False and store.get_item("ro:x")["state"] == "downloading")


# ---------------------------------------------------------------- end to end, through the real tools

def test_end_to_end():
    store = Store()          # the default store: the same one the real tools open
    demo.start(store)
    try:
        res = service.control("pause", id="demo:climb", store=store)
        check("dl_pause on a demo row goes through the real tool and is pending", res["ok"] and res["pending"] == ["demo:climb"])
        check("and the row is paused", store.get_item("demo:climb")["state"] == "paused")
        res = service.control("pause", id="demo:climb", store=store)
        # The service refuses first: a paused row no longer offers Pause, so the demo tool is not even asked.
        check("pausing it again is refused (the paused row no longer offers Pause)", res["ok"] is False and "can't pause a download that is paused" in res["error"] and "read-only" not in res["error"])
        res = service.control("resume", id="demo:climb", store=store)
        check("dl_resume works", res["ok"] and store.get_item("demo:climb")["state"] == "downloading")
        res = service.control("resume", id="demo:climb", store=store)
        check("resuming a row that is running is refused (a running row does not offer Resume)", res["ok"] is False and "can't resume a download that is downloading" in res["error"] and "read-only" not in res["error"])
        res = service.control("cancel", id="demo:indeterminate", store=store)
        check("dl_cancel works", res["ok"] and store.get_item("demo:indeterminate")["state"] == "cancelled")
        res = service.control("retry", id="demo:indeterminate", store=store)
        check("a verb the demo did not declare (retry) is refused in plain words, not as read-only", res["ok"] is False and "can't retry a download that is cancelled" in res["error"] and "read-only" not in res["error"])

        direct = tools.execute_tool("hub_demo_pause", {"id": "steam:99"})
        check("calling the tool directly on another provider's id is refused", direct.get("ok") is False and "not a demo download" in direct["error"])
        check("calling a tool with no arguments is refused, not an exception", tools.execute_tool("hub_demo_cancel", {}).get("ok") is False)
        res = service.control("pause", provider="demo", store=store)
        check("a bulk call reaches demo rows only through the demo's own tool (what can be paused is, the rest is skipped)",
              res["ok"] and "demo:game" in res["pending"] and store.get_item("demo:game")["state"] == "paused"
              and any(s["id"] == "demo:indeterminate" for s in res["skipped"]))
        res = service.control("cancel", all=True, store=store)
        check("cancel-all cancels demo rows too (it runs the demo's cancel tool per row)",
              res["ok"] and "demo:game" in res["pending"] and store.get_item("demo:game")["state"] == "cancelled")
    finally:
        demo.clear(store)


try:
    test_declared()
    test_rows_at()
    test_verbs()
    test_refusals()
    test_isolation()
    test_end_to_end()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
