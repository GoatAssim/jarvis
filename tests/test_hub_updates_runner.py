"""Download Hub, N23 core: the UpdateRunner and `updates.apply_answer` / `present` / `request_check`.

Tested against a FAKE update tool and a FAKE clock: nothing here sleeps for a minute, nothing touches the
network, no real package manager or Steam is needed (Steam is a folder of fixture manifests).

Covers: the first check a few minutes after start-up (not before), then every update_interval_h (declared
starting value, then the owner's setting); one check at a time per provider ("already checking"); the 5 minute
manual limit; a failed / raised / offline check keeping the old findings, showing their age and naming what
failed; a partial answer (one manager failed) not erasing that manager's findings; the 60 second timeout and a
hung check never being stacked; a provider switched off or with update_checks off not being checked and showing
nothing; a provider that stops declaring its update tool; the real thread; the daemon starting and stopping the
runner; and the proof that a fake 60-second check does NOT delay a Steam poll.

Run: python tests/test_hub_updates_runner.py
"""
import os
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-upd-run-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import providers, steam, updates  # noqa: E402
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


GOOD = {"updates": [{"id": "a", "title": "Alpha", "installed": "1", "available": "2", "source": "reg"}], "complete": True}


class Env:
    """A runner on a fake clock with a fake update tool."""

    def __init__(self, interval=None, answer=GOOD, first=180.0, tool="check_fetch_updates"):
        self.clock = Clock()
        self.store = new_store(self.clock)
        self.store.register_provider("fetch", label="Fetch", mode="push")
        self.calls = []
        self.answer = answer
        self.declared = [{"id": "fetch", "update_tool": tool, "update_interval_h": interval}]
        self.t0 = self.clock()
        self.runner = updates.UpdateRunner(self.store, clock=self.clock, executor=self.execute,
                                           declared=lambda: self.declared, first_delay_s=first)

    def execute(self, name, args):
        self.calls.append((name, args))
        answer = self.answer
        if isinstance(answer, BaseException):
            raise answer
        return answer() if callable(answer) else answer

    def beat(self):
        self.store.daemon_beat(os.getpid(), self.clock())

    def settle(self):
        chk = self.runner._state["fetch"]["check"]
        if chk is not None:
            assert chk.done.wait(5), "the fake check did not finish"
        return self.runner.tick()

    def run_check(self):
        acted = self.runner.tick()
        self.settle()
        return acted

    def present(self):
        return updates.present(self.store, now=self.clock())

    def titles(self):
        return sorted(f["title"] for f in self.present()["updates"])


# --------------------------------------------------------------------------- schedule

def test_schedule():
    env = Env()
    env.runner.tick()
    state = env.store.update_check_state()["fetch"]
    check("start-up registers the provider's update tool in the store", state["update_tool"] == "check_fetch_updates")
    env.clock.advance(100)
    check("nothing is checked before the first delay", env.runner.tick() == [] and env.calls == [])
    env.clock.advance(81)
    acted = env.run_check()
    check("the first check comes a few minutes after start-up", env.calls == [("check_fetch_updates", {})] and "fetch" in acted)
    check("its findings are stored and shown", env.titles() == ["Alpha"])
    state = env.store.update_check_state()["fetch"]
    check("the state records a clean check", state["ok_at"] == env.clock() and state["checked_at"] == env.clock()
          and state["error"] is None and state["running_since"] is None and not state["problems"])
    env.clock.advance(86399)
    env.runner.tick()
    check("then every 24 h by default: not yet at 23 h 59 min", len(env.calls) == 1)
    env.clock.advance(2)
    env.run_check()
    check("and again after 24 h", len(env.calls) == 2)

    two = Env(interval=2)
    two.clock.advance(181)
    two.run_check()
    two.clock.advance(7199)
    two.runner.tick()
    check("a declared update_interval_h is the starting interval", len(two.calls) == 1)
    two.clock.advance(2)
    two.run_check()
    check("...and is honoured", len(two.calls) == 2)

    owner = Env(interval=2)
    owner.store.update_provider("fetch", settings={"update_interval_h": 1})
    owner.clock.advance(181)
    owner.run_check()
    owner.clock.advance(3601)
    owner.run_check()
    check("the owner's update_interval_h setting beats the declared one", len(owner.calls) == 2)


def test_no_update_tool():
    env = Env(tool=None)
    env.declared = [{"id": "fetch"}]
    env.clock.advance(1000)
    env.runner.tick()
    check("a provider without an update_tool is never checked", env.calls == []
          and not (env.store.update_check_state().get("fetch") or {}).get("update_tool"))
    check("and has no health line", env.present()["providers"] == [])

    env = Env()
    env.clock.advance(181)
    env.run_check()
    env.declared = [{"id": "fetch"}]
    env.clock.advance(100000)
    env.runner.tick()
    check("a provider that stops declaring its update tool loses it and is not checked again",
          len(env.calls) == 1 and env.store.update_check_state()["fetch"]["update_tool"] is None)


# --------------------------------------------------------------------------- one at a time, manual limit

def test_one_at_a_time_and_manual():
    gate = threading.Event()

    def blocked():
        gate.wait(10)
        return GOOD

    env = Env(answer=blocked)
    env.clock.advance(181)
    env.runner.tick()
    env.beat()
    again = env.runner.tick()
    check("one check at a time per provider: a second tick starts nothing", len(env.calls) <= 1 and again == [])
    res = updates.request_check(env.store, "fetch", now=env.clock())
    check("a manual request while one runs is 'already checking'", res["ok"] and res["results"][0]["status"] == "already_checking")
    check("and the provider shows as checking", env.present()["providers"][0]["status"] == "checking")
    gate.set()
    env.settle()
    check("the check ends and clears its running mark", env.store.update_check_state()["fetch"]["running_since"] is None
          and len(env.calls) == 1)

    env = Env()
    env.clock.advance(181)
    env.run_check()
    env.beat()
    res = updates.request_check(env.store, "fetch", now=env.clock())
    check("a manual check is accepted", res["ok"] and res["results"][0]["status"] == "requested")
    env.runner.tick()
    env.settle()
    check("the runner picks it up at once, long before the interval", len(env.calls) == 2)
    env.beat()
    res = updates.request_check(env.store, "fetch", now=env.clock())
    check("a second manual check inside 5 minutes is too_soon", res["results"][0]["status"] == "too_soon"
          and "min" in res["results"][0]["message"])
    check("and starts nothing", env.runner.tick() == [] and len(env.calls) == 2)
    env.clock.advance(301)
    env.beat()
    res = updates.request_check(env.store, "fetch", now=env.clock())
    env.runner.tick()
    env.settle()
    check("after 5 minutes a manual check runs again", res["results"][0]["status"] == "requested" and len(env.calls) == 3)

    all_res = updates.request_check(env.store, None, now=env.clock() + 1000)
    check("no provider named = every provider that can be checked", all_res["ok"] and [r["provider"] for r in all_res["results"]] == ["fetch"])


def test_request_refusals():
    env = Env()
    env.runner.tick()
    env.clock.advance(60)
    res = updates.request_check(env.store, "fetch", now=env.clock())
    check("with the daemon not running a check is refused, and says so", res["ok"] is False and res["code"] == "daemon_down")
    env.beat()
    res = updates.request_check(env.store, "nope", now=env.clock())
    check("an unknown provider is named in the refusal", res["ok"] is False and "no provider called nope" in res["error"])
    env.store.register_provider("plain", label="Plain", mode="push")
    res = updates.request_check(env.store, "plain", now=env.clock())
    check("a provider with no update check is refused", res["ok"] is False and "no update check" in res["error"])
    env.store.update_provider("fetch", settings={"update_checks": False})
    res = updates.request_check(env.store, "fetch", now=env.clock())
    check("update_checks off is refused", res["ok"] is False and "switched off" in res["error"])
    env.store.update_provider("fetch", settings={"update_checks": True})
    env.store.set_provider_enabled("fetch", False)
    res = updates.request_check(env.store, "fetch", now=env.clock())
    check("a switched-off provider is refused", res["ok"] is False and "switched off" in res["error"])
    res = updates.request_check(env.store, None, now=env.clock())
    check("with nothing checkable and no name: ok, an empty list, a message", res["ok"] and res["results"] == [] and res["message"])


# --------------------------------------------------------------------------- failure, partial, timeout

def test_failures_keep_findings():
    env = Env()
    env.clock.advance(181)
    env.run_check()
    ok_at = env.clock()
    env.answer = {"error": "offline"}
    env.clock.advance(86401)
    env.run_check()
    out = env.present()
    check("a failed check keeps the old findings", env.titles() == ["Alpha"])
    check("and the provider says it failed", out["providers"][0]["status"] == "failed")
    check("in words, with what failed and how old the findings are",
          len(out["problems"]) == 1 and "offline" in out["problems"][0] and "1 day ago" in out["problems"][0]
          and out["problems"][0].startswith("Fetch:"))
    state = env.store.update_check_state()["fetch"]
    check("the clean-check time did not move", state["ok_at"] == ok_at and state["checked_at"] == ok_at and state["error"] == "offline")
    check("each finding shows its own age", out["updates"][0]["age_s"] >= 86400)

    env.answer = RuntimeError("boom")
    env.clock.advance(1801)
    env.run_check()
    check("a tool that raises is a recorded failure, not a crash", "boom" in env.store.update_check_state()["fetch"]["error"]
          and env.titles() == ["Alpha"])
    check("a failed check is retried after 30 min, not after the 24 h interval", len(env.calls) == 3)
    env.answer = "this is not json"
    env.clock.advance(90000)
    env.run_check()
    check("an answer that isn't JSON is a failure", "not JSON" in env.store.update_check_state()["fetch"]["error"])
    env.answer = {"nothing": 1}
    env.clock.advance(90000)
    env.run_check()
    check("an answer with no 'updates' list is a failure and clears nothing", "no 'updates' list" in env.store.update_check_state()["fetch"]["error"]
          and env.titles() == ["Alpha"])
    env.answer = {"updates": [{"id": "x", "title": "X"}], "ok": False}
    env.clock.advance(90000)
    env.run_check()
    check("ok: false is a failure", env.store.update_check_state()["fetch"]["error"] and env.titles() == ["Alpha"])
    env.answer = {"updates": [{"id": "pad", "title": "x" * 70000}], "complete": True}
    env.clock.advance(90000)
    env.run_check()
    check("an answer over the size limit is dropped whole", "KB" in env.store.update_check_state()["fetch"]["error"]
          and env.titles() == ["Alpha"])

    env.answer = GOOD
    env.clock.advance(90000)
    env.run_check()
    out = env.present()
    check("the next good check recovers: no error, no problems", out["providers"][0]["status"] == "ok" and out["problems"] == []
          and env.store.update_check_state()["fetch"]["error"] is None)


def test_partial_answer():
    env = Env(answer={"updates": [{"id": "w", "title": "W", "source": "winget"}, {"id": "n", "title": "N", "source": "npm"},
                                  {"id": "o", "title": "O", "source": "npm"}], "complete": True})
    env.clock.advance(181)
    env.run_check()
    check("three findings from two managers", env.titles() == ["N", "O", "W"])
    env.answer = {"updates": [{"id": "n", "title": "N", "source": "npm"}], "complete": True,
                  "problems": [{"source": "winget", "error": "timed out after 60 s"}]}
    env.clock.advance(86401)
    env.run_check()
    check("npm's finished list clears npm's gone finding; winget's findings stay (it could not be asked)", env.titles() == ["N", "W"])
    out = env.present()
    check("the failed manager is named in words", out["providers"][0]["status"] == "partial"
          and out["problems"] == ["Fetch: winget: timed out after 60 s"])
    state = env.store.update_check_state()["fetch"]
    check("a partial check counts as checked but not as clean", state["checked_at"] == env.clock() and state["ok_at"] != env.clock())
    env.answer = {"updates": [{"id": "n", "title": "N", "source": "npm"}], "complete": True, "problems": ["something broke"]}
    env.clock.advance(86401)
    env.run_check()
    check("a problem that names no source means nothing is cleared", env.titles() == ["N", "W"])
    env.answer = {"updates": [{"id": "n", "title": "N", "source": "npm"}], "complete": True}
    env.clock.advance(86401)
    env.run_check()
    check("a clean complete answer clears the rest", env.titles() == ["N"] and env.present()["problems"] == [])
    env.answer = {"updates": [], "complete": True}
    env.clock.advance(86401)
    env.run_check()
    check("an empty complete answer means 'nothing is out of date'", env.titles() == [] and env.present()["providers"][0]["status"] == "ok")
    env.answer = {"updates": [{"id": "z", "title": "Z"}]}
    env.clock.advance(86401)
    env.run_check()
    env.answer = {"updates": []}
    env.clock.advance(86401)
    env.run_check()
    check("without `complete` an empty list clears nothing", env.titles() == ["Z"])


def test_timeout_and_hung_check():
    gate = threading.Event()

    def hang():
        gate.wait(30)
        return GOOD

    env = Env(answer=hang)
    env.store.update_offer("fetch", "old", {"title": "Old one", "installed": "1", "available": "2"})
    env.clock.advance(181)
    env.runner.tick()
    chk = env.runner._state["fetch"]["check"]
    check("the check is running in a thread of its own", chk is not None and chk.thread is not threading.current_thread()
          and chk.thread.name == "hub-update-check-fetch")
    env.clock.advance(59)
    env.runner.tick()
    check("at 59 s it is still running", env.runner._state["fetch"]["check"] is chk and not chk.done.is_set())
    env.clock.advance(2)
    acted = env.runner.tick()
    state = env.store.update_check_state()["fetch"]
    check("after 60 s it is given up on, with the reason in words",
          "fetch" in acted and "longer than 60 s" in state["error"] and env.runner._state["fetch"]["stuck"] is chk)
    check("the old findings stay and the problem is shown", env.titles() == ["Old one"]
          and "longer than 60 s" in env.present()["problems"][0])
    env.clock.advance(100000)
    env.runner.tick()
    check("a hung check is never stacked with a second one", len(env.calls) == 1)
    gate.set()
    check("(the hung thread ends)", chk.done.wait(5))
    env.runner.tick()
    env.settle()
    check("once it has ended the provider is checked again", len(env.calls) == 2 and env.store.update_check_state()["fetch"]["error"] is None
          and env.store.update_check_state()["fetch"]["running_since"] is None)


# --------------------------------------------------------------------------- switched off

def test_switched_off():
    env = Env()
    env.clock.advance(181)
    env.run_check()
    check("a finding is showing", env.titles() == ["Alpha"])
    env.store.set_provider_enabled("fetch", False)
    out = env.present()
    check("a switched-off provider shows nothing: no findings, no health line, no problems",
          out["count"] == 0 and out["providers"] == [] and out["problems"] == [])
    env.clock.advance(200000)
    env.runner.tick()
    check("and is not checked", len(env.calls) == 1)
    env.store.set_provider_enabled("fetch", True)
    env.store.update_provider("fetch", settings={"update_checks": False})
    out = env.present()
    check("update_checks off: not checked, and shows nothing", out["count"] == 0 and out["providers"] == [] and env.runner.tick() == []
          and len(env.calls) == 1)
    env.store.update_provider("fetch", settings={"update_checks": True})
    env.runner.tick()
    env.settle()
    check("switched back on it is checked and shows again (nothing was deleted)", len(env.calls) == 2 and env.titles() == ["Alpha"])


# --------------------------------------------------------------------------- present / dismiss

def test_present_and_dismiss():
    env = Env()
    env.runner.tick()
    check("before any check the provider says so", env.present()["providers"][0]["status"] == "never"
          and env.present()["count"] == 0 and env.present()["checked_at"] is None)
    env.clock.advance(181)
    env.run_check()
    out = env.present()
    row = out["updates"][0]
    check("a finding carries what a panel needs", row["provider"] == "fetch" and row["provider_label"] == "Fetch" and row["id"] == "a"
          and row["installed"] == "1" and row["available"] == "2" and row["age_s"] == 0 and out["checked_at"] == env.clock())
    res = updates.dismiss_finding(env.store, "fetch", "a")
    check("Ignore this version hides it and says how long", res["ok"] and "different version" in res["note"] and env.titles() == [])
    check("a hidden finding can still be listed for an 'ignored' view",
          updates.present(env.store, now=env.clock(), include_hidden=True)["updates"][0]["hidden"] is True)
    check("ignoring something that isn't there is a refusal", updates.dismiss_finding(env.store, "fetch", "zzz")["ok"] is False)
    env.answer = {"updates": [{"id": "a", "title": "Alpha", "installed": "1", "available": "3"}], "complete": True}
    env.clock.advance(86401)
    env.run_check()
    check("a different available (2 -> 3) from the next check shows it again", env.titles() == ["Alpha"])
    check("count is the badge", updates.count(env.store, now=env.clock()) == 1)
    check("a filter for another provider shows nothing", updates.present(env.store, provider="other", now=env.clock())["count"] == 0)
    for age, words in ((None, "never"), (5, "a moment ago"), (125, "2 min ago"), (7300, "2 h ago"), (86400, "1 day ago"), (200000, "2 days ago")):
        check("age_words(%r) is %r" % (age, words), updates.age_words(age) == words)


# --------------------------------------------------------------------------- the real thread and the daemon

def test_real_thread():
    store = new_store()
    store.register_provider("fetch", label="Fetch", mode="push")
    calls = []

    def run(name, args):
        calls.append(name)
        return GOOD

    runner = updates.UpdateRunner(store, executor=run, declared=lambda: [{"id": "fetch", "update_tool": "check_fetch_updates"}],
                                  first_delay_s=0, loop_s=0.05)
    thread = runner.start()
    for _ in range(100):
        if calls and store.list_updates():
            break
        time.sleep(0.05)
    check("the runner's own thread runs the check", thread.name == "hub-updates" and calls == ["check_fetch_updates"]
          and [f["title"] for f in store.list_updates()] == ["Alpha"])
    runner.stop()
    check("stop ends the thread", not thread.is_alive())
    check("start twice is one thread", runner.start() is runner.start())
    runner.stop()


class FakeRunner:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self, *a, **k):
        self.stopped += 1


def test_daemon_starts_and_stops_it():
    store = new_store()
    daemon = HubDaemon(store)
    daemon.declared_providers = []
    fake = FakeRunner()
    daemon._updates = fake
    os.environ.pop("JARVIS_HUB_NO_UPDATES", None)
    try:
        daemon.start_up()
        check("the daemon starts the update runner in start_up", fake.started == 1)
        daemon.stop(timeout=1)
        check("and stops it in stop", fake.stopped == 1)
        os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
        other = HubDaemon(new_store())
        other.declared_providers = []
        other._updates = FakeRunner()
        other.start_up()
        check("JARVIS_HUB_NO_UPDATES keeps it from starting", other._updates.started == 0)
        other.stop(timeout=1)
    finally:
        os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
    check("the daemon's runner is an UpdateRunner of its own", isinstance(HubDaemon(new_store())._updates, updates.UpdateRunner))
    twin = HubDaemon(new_store())
    check("and a different object from the poll runner", twin._updates is not twin._poller)


# --------------------------------------------------------------------------- a slow check does not delay a Steam poll

def acf(appid, name, flags, to_dl=0, got=0, size=0):
    return textwrap.dedent('''\
        "AppState"
        {
        \t"appid"\t\t"%s"
        \t"name"\t\t"%s"
        \t"StateFlags"\t\t"%d"
        \t"installdir"\t\t"%s"
        \t"LastUpdated"\t\t"0"
        \t"SizeOnDisk"\t\t"%d"
        \t"BytesToDownload"\t\t"%d"
        \t"BytesDownloaded"\t\t"%d"
        \t"BytesToStage"\t\t"0"
        \t"BytesStaged"\t\t"0"
        }
        ''') % (appid, name, flags, name, size, to_dl, got)


def test_slow_check_does_not_delay_steam_poll():
    base = Path(HOME) / "steamcase"
    steam_dir = base / "Steam"
    (steam_dir / "steamapps" / "common").mkdir(parents=True)
    (steam_dir / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "%s" } }' % str(steam_dir).replace("\\", "\\\\"))
    (steam_dir / "steamapps" / "appmanifest_100.acf").write_text(acf(100, "Fresh Install", 1026, to_dl=1000, got=200))

    clock = Clock()
    store = new_store(clock)
    prov = steam.SteamProvider()
    prov.root_override = str(steam_dir)
    prov.is_running = lambda: True
    steam.PROVIDER = prov
    providers.BUILTIN.clear()
    providers.BUILTIN["steam"] = prov
    providers.sync_declared(store, declared=[])
    store.register_provider("fetch", label="Fetch", mode="push")

    started = threading.Event()
    release = threading.Event()

    def sixty_second_check(name, args):
        started.set()
        release.wait(60)               # stands in for a check that takes a minute: ends when the TEST says so
        return GOOD

    checker = updates.UpdateRunner(store, clock=clock, executor=sixty_second_check, first_delay_s=0,
                                   declared=lambda: [{"id": "fetch", "update_tool": "check_fetch_updates"}])
    poll = providers.PollRunner(store, clock=clock, executor=lambda n, a: {})

    def poll_now():
        poll.tick()
        for _ in range(100):
            fut = poll._state["steam"]["future"]
            if fut is None or fut.done():
                break
            time.sleep(0.02)
        poll.tick()

    t0 = time.monotonic()
    checker.tick()
    check("the slow check has started, in its own thread", started.wait(5) and checker._state["fetch"]["check"].thread is not threading.current_thread())
    t_tick = time.monotonic()
    checker.tick()
    check("the update runner's own pass returns at once while the check hangs", time.monotonic() - t_tick < 1.0)
    poll_now()
    row = store.get_item("steam:100")
    check("a Steam poll runs and makes its row while the check is still going", row is not None and row["state"] == "downloading"
          and not release.is_set() and not checker._state["fetch"]["check"].done.is_set())
    check("it took real milliseconds, nothing like the check's minute", time.monotonic() - t0 < 10.0)

    (steam_dir / "steamapps" / "appmanifest_100.acf").write_text(acf(100, "Fresh Install", 4, size=1000))
    clock.advance(30)
    poll_now()
    check("a second poll 30 fake seconds later also runs on time (the install finished)",
          store.get_item("steam:100")["state"] != "downloading")
    clock.advance(31)
    checker.tick()
    check("at 61 fake seconds the check is given up on, and the poll side never noticed",
          "longer than 60 s" in store.update_check_state()["fetch"]["error"])
    release.set()
    stuck = checker._state["fetch"]["stuck"]
    check("(the hung thread ends)", stuck.done.wait(5))
    providers.BUILTIN.clear()


if __name__ == "__main__":
    for fn in (test_schedule, test_no_update_tool, test_one_at_a_time_and_manual, test_request_refusals,
               test_failures_keep_findings, test_partial_answer, test_timeout_and_hung_check, test_switched_off,
               test_present_and_dismiss, test_real_thread, test_daemon_starts_and_stops_it,
               test_slow_check_does_not_delay_steam_poll):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
