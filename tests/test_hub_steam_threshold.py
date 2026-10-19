"""Download Hub: the Steam update-size notification threshold (plan decision 8, phase 8).

Steam UPDATES show in the table like installs but are announced only when they are big enough; the
size is the Steam provider's own setting `notify_min_update_gb` (default 1 GB, 0 = every update),
changed with `dl_provider_prefs`.  An install of a new game follows the normal `notify.done` rules.

Covers: the setting's default, validator and fallback; `dl_provider_prefs` (service.update_provider)
range-checking it and `dl_providers` showing the effective value; the Dispatcher announcing a big
update and staying quiet for a small one; an install, a failure, another provider and an unknown
size; the threshold changing between two passes; the queue-empty tally; a daemon restart and a
provider switched off announcing nothing twice / at all.

Run: python tests/test_hub_steam_threshold.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-steamthr-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import notify, service, settings as hs  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

GB = 1024 ** 3
MB = 1024 ** 2
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


class Sink:
    """Stands in for notifier.notify and records every message."""

    def __init__(self):
        self.sent = []

    def __call__(self, title, message, *, level=None, failed=False, channels=None, look=None, tone=None):
        self.sent.append({"title": title, "message": message, "level": level, "failed": failed,
                          "channels": channels, "look": look, "tone": tone})
        return {"delivered": ["inbox"], "failed_channels": []}


def fixture(clock=None):
    """(store, sink, dispatcher, clock) with a Steam provider registered and the baseline taken."""
    _n[0] += 1
    clock = clock or Clock()
    store = Store(Path(HOME) / f"steam-{_n[0]}.db", clock=clock)
    store.register_provider("steam", label="Steam", mode="poll", kind="game", settings={"root": ""})
    sink = Sink()
    disp = notify.Dispatcher(store, deliver=sink, clock=clock)
    disp.since()
    clock.advance(1)
    return store, sink, disp, clock


def steam_row(store, clock, appid, name, subtitle, size, state="done", provider="steam"):
    """A Steam row as the poller reports it: it was downloading, then is done."""
    fields = {"title": name, "subtitle": subtitle, "kind": "game"}
    if size:
        fields.update(bytes_total=size, bytes_done=size // 2, percent=50.0)
    store.upsert_provider_row(provider, appid, dict(fields, state="downloading"))
    clock.advance(5)
    final = {"state": state}
    if state == "done":
        final.update(percent=100)
    elif state == "failed":
        final.update(error="disk full")
    store.upsert_provider_row(provider, appid, final)
    return store.get_item("%s:%s" % (provider, appid))


def run(disp, store, clock, passes=2):
    """Two passes with the coalescing window between them, so everything due is sent."""
    for _ in range(passes):
        disp.scan(store.get_settings())
        clock.advance(30)


def titles(sink):
    return [m["title"] for m in sink.sent]


# ------------------------------------------------------------------ the setting

def test_setting():
    check("default threshold is 1 GB", hs.STEAM_MIN_UPDATE_GB_DEFAULT == 1.0)
    check("an unset threshold reads as the default (a provider row made before the key existed)",
          hs.provider_setting("steam", {"root": ""}, "notify_min_update_gb") == 1.0
          and hs.steam_min_update_bytes({}) == GB and hs.steam_min_update_bytes(None) == GB)
    check("0 means every update", hs.steam_min_update_bytes({"notify_min_update_gb": 0}) == 0)
    check("a stored value is used", hs.steam_min_update_bytes({"notify_min_update_gb": 2.5}) == int(2.5 * GB))
    check("a stored value that no longer validates falls back to the default, never breaks a pass",
          hs.steam_min_update_bytes({"notify_min_update_gb": "garbage"}) == GB
          and hs.steam_min_update_bytes({"notify_min_update_gb": -4}) == GB)

    clean, errs = hs.validate_provider_settings("steam", {"notify_min_update_gb": "2.5", "root": "D:/Steam"})
    check("a number (even as text) is accepted and normalised, Steam's root passes through untouched",
          clean == {"notify_min_update_gb": 2.5, "root": "D:/Steam"} and not errs)
    check("0 is accepted", hs.validate_provider_settings("steam", {"notify_min_update_gb": 0})[0] == {"notify_min_update_gb": 0.0})
    for bad in (-1, "abc", True, None, float("nan"), float("inf"), 10_001, [1], {"a": 1}):
        clean, errs = hs.validate_provider_settings("steam", {"notify_min_update_gb": bad})
        check("refused: %r" % (bad,), errs and "notify_min_update_gb" not in clean)
    clean, errs = hs.validate_provider_settings("playnite", {"notify_min_update_gb": 1})
    check("another provider can't take Steam's setting (no silent no-op)", errs and "steam" in errs[0])
    clean, errs = hs.validate_provider_settings("my_tool", {"anything": 1})
    check("a tool's own keys still pass", clean == {"anything": 1} and not errs)
    check("the owner-facing view fills the default in and leaves the root out",
          hs.owner_provider_settings("steam", {"root": "D:/Steam"}) == {"notify_min_update_gb": 1.0}
          and hs.owner_provider_settings("playnite", {"x": 1}) == {})


# ------------------------------------------------------------------ dl_provider_prefs / dl_providers

def test_service():
    store, sink, disp, clock = fixture()
    row = [p for p in service.list_providers(store)["providers"] if p["id"] == "steam"][0]
    check("dl_providers shows the effective threshold even though the stored block has none",
          row["settings"] == {"notify_min_update_gb": 1.0})
    check("...and not Steam's folder", "root" not in row["settings"])

    res = service.update_provider("steam", settings={"notify_min_update_gb": "3"}, store=store)
    check("setting it works", res["ok"] is True and store.get_provider("steam")["settings"]["notify_min_update_gb"] == 3.0)
    check("...and keeps Steam's own folder setting", store.get_provider("steam")["settings"].get("root") == "")
    row = [p for p in res["providers"] if p["id"] == "steam"][0]
    check("the reply carries the new value", row["settings"]["notify_min_update_gb"] == 3.0)

    res = service.update_provider("steam", enabled=False, settings={"notify_min_update_gb": -1}, store=store)
    check("a bad value is refused with its reason", res["ok"] is False and "between 0 and" in res["error"])
    check("...and nothing else in that call was applied (the provider is still on, the value unchanged)",
          store.get_provider("steam")["enabled"] is True and store.get_provider("steam")["settings"]["notify_min_update_gb"] == 3.0)
    res = service.update_provider("steam", settings={"notify_min_update_gb": "lots"}, store=store)
    check("text is refused", res["ok"] is False)
    store.register_provider("playnite", label="Playnite", mode="push", kind="game")
    res = service.update_provider("playnite", settings={"notify_min_update_gb": 1}, store=store)
    check("Playnite can't take it", res["ok"] is False)
    res = service.update_provider("steam", settings={"root": "D:/Steam"}, store=store)
    check("the old use (Steam's root) still works", res["ok"] is True and store.get_provider("steam")["settings"]["root"] == "D:/Steam")


# ------------------------------------------------------------------ the notifications

def test_big_and_small():
    store, sink, disp, clock = fixture()
    steam_row(store, clock, "1", "Big Game", "update", 3 * GB)
    steam_row(store, clock, "2", "Small Game", "update", 200 * MB)
    run(disp, store, clock)
    msgs = [m for m in sink.sent if m["title"] == "Game updated"]
    check("a big update is announced, in the game wording", len(msgs) == 1 and "Big Game finished updating" in msgs[0]["message"])
    check("...with its size", "3.0 GB" in msgs[0]["message"])
    check("a small update is not announced", not [m for m in sink.sent if "Small Game" in m["message"]])
    check("a small update isn't in the 'all finished' tally either", disp._burst["done"] in (0, 1))


def test_install_follows_notify_done():
    store, sink, disp, clock = fixture()
    steam_row(store, clock, "3", "Hades II", "install", 150 * MB)
    run(disp, store, clock)
    inst = [m for m in sink.sent if m["title"] == "Game installed"]
    check("an install is announced however small (it follows notify.done)",
          len(inst) == 1 and inst[0]["message"].startswith("Hades II finished installing"))

    store, sink, disp, clock = fixture()
    store.set_settings({"notify.done.enabled": False})
    steam_row(store, clock, "4", "Quiet Install", "install", 5 * GB)
    steam_row(store, clock, "5", "Quiet Update", "update", 5 * GB)
    run(disp, store, clock)
    check("with notify.done off neither an install nor even a big update is announced", not sink.sent)


def test_threshold_changes():
    store, sink, disp, clock = fixture()
    service.update_provider("steam", settings={"notify_min_update_gb": 0}, store=store)
    steam_row(store, clock, "6", "Tiny Update", "update", 20 * MB)
    run(disp, store, clock)
    check("0 announces every update", [m for m in sink.sent if "Tiny Update" in m["message"]])

    store, sink, disp, clock = fixture()
    service.update_provider("steam", settings={"notify_min_update_gb": 5}, store=store)
    steam_row(store, clock, "7", "Mid Update", "update", 3 * GB)
    run(disp, store, clock)
    check("a raised threshold keeps a 3 GB update quiet", not sink.sent)

    # the owner changes it between two passes: the next pass uses the new value
    store, sink, disp, clock = fixture()
    disp.scan(store.get_settings())
    steam_row(store, clock, "8", "Later Update", "update", 500 * MB)
    service.update_provider("steam", settings={"notify_min_update_gb": 0.25}, store=store)
    run(disp, store, clock)
    check("a threshold changed between passes is read fresh", [m for m in sink.sent if "Later Update" in m["message"]])


def test_edges():
    store, sink, disp, clock = fixture()
    steam_row(store, clock, "9", "Empty Update", "update", 0)
    run(disp, store, clock)
    check("an update with no known size is quiet at the default threshold", not sink.sent)
    service.update_provider("steam", settings={"notify_min_update_gb": 0}, store=store)
    steam_row(store, clock, "10", "Empty Update Two", "update", 0)
    run(disp, store, clock)
    check("...and announced when the owner asked for every update", [m for m in sink.sent if "Empty Update Two" in m["message"]])

    store, sink, disp, clock = fixture()
    steam_row(store, clock, "11", "Broken Update", "update", 100 * MB, state="failed")
    run(disp, store, clock)
    check("a failed small update is still announced (the threshold is about finishes only)",
          [m for m in sink.sent if m["failed"] and "Broken Update" in m["message"]])

    store, sink, disp, clock = fixture()
    store.register_provider("playnite", label="Playnite", mode="push", kind="game")
    steam_row(store, clock, "12", "Other Store Update", "update", 50 * MB, provider="playnite")
    run(disp, store, clock)
    check("another provider's update is never held back by Steam's threshold",
          [m for m in sink.sent if "Other Store Update" in m["message"]])

    store, sink, disp, clock = fixture()
    store.set_settings({"notify.providers": False})
    steam_row(store, clock, "13", "Muted Update", "update", 9 * GB)
    run(disp, store, clock)
    check("notify.providers off still silences everything from providers, big or not", not sink.sent)

    check("hub downloads are never called an install or an update",
          notify.game_action({"owner": "hub", "kind": "game", "subtitle": "update"}) == ""
          and notify.below_update_threshold({"owner": "hub", "provider": "steam", "kind": "game",
                                             "subtitle": "update", "bytes_total": 5}, {}) is False)


def test_once_and_off():
    store, sink, disp, clock = fixture()
    steam_row(store, clock, "14", "Once Update", "update", 4 * GB)
    run(disp, store, clock)
    first = len(sink.sent)
    again = notify.Dispatcher(store, deliver=sink, clock=clock)         # a daemon restart
    run(again, store, clock)
    check("a big update is announced once, never again after a restart", first == 1 and len(sink.sent) == 1)

    store, sink, disp, clock = fixture()
    steam_row(store, clock, "15", "Off Update", "update", 4 * GB)
    store.set_provider_enabled("steam", False)
    run(disp, store, clock)
    check("a provider switched off sends nothing", not sink.sent)


def main():
    for fn in (test_setting, test_service, test_big_and_small, test_install_follows_notify_done,
               test_threshold_changes, test_edges, test_once_and_off):
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
