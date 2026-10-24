"""Download Hub, brief 31 (TRACK_C 17.6): a notification switch per provider, `notify.by_provider.<id>`.

Covers: the key is an ordinary writable bool (steam and playnite out of the box, any other well-formed
provider id accepted, an odd id refused), in both tiers; it is not mistaken for an event switch
(`notify.<event>.enabled`), so the Settings page shows it as a generic row; a provider switched off says
nothing for a finish or a failure and is left out of the queue-empty tally; a provider with no entry, or
switched on, still speaks; the master `notify.providers` switch still silences all of them; one
provider's switch does not touch another's or the hub's own downloads; the switch is read on every pass.

Run: python tests/test_hub_notify_by_provider.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-nbp-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import notify, providers, service, settings as hs  # noqa: E402
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
    def __init__(self, t=3_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, title, message, *, level=None, failed=False, channels=None, look=None, tone=None):
        self.sent.append({"title": title, "message": message, "failed": failed})
        return {"delivered": ["inbox"], "failed_channels": []}


def fixture():
    _n[0] += 1
    clock = Clock()
    store = Store(Path(HOME) / f"nbp-{_n[0]}.db", clock=clock)
    for pid in ("steam", "playnite", "mytool"):
        store.register_provider(pid, label=pid.title(), mode="poll", kind="game", settings={})
    sink = Sink()
    disp = notify.Dispatcher(store, deliver=sink, clock=clock)
    disp.since()
    clock.advance(1)
    return store, sink, disp, clock


def finish(store, clock, provider, key, name, state="done"):
    store.upsert_provider_row(provider, key, {"title": name, "kind": "game", "state": "downloading",
                                              "bytes_total": 5 * 1024 ** 3, "bytes_done": 1024 ** 3, "percent": 20.0})
    clock.advance(5)
    final = {"state": state}
    if state == "done":
        final["percent"] = 100
    else:
        final["error"] = "disk full"
    store.upsert_provider_row(provider, key, final)


def run(disp, store, clock, passes=2):
    for _ in range(passes):
        disp.scan(store.get_settings())
        clock.advance(30)


def titles(sink):
    return " | ".join(m["title"] + " " + m["message"] for m in sink.sent)


# ---------------------------------------------------------------- the key
check("steam and playnite have a switch out of the box, on", hs.merged({})["notify"]["by_provider"] == {"steam": True, "playnite": True})
check("the built-in ids are exactly the built-in providers", set(hs.NOTIFY_PROVIDERS) == set(providers.builtin_providers()))
for pid in hs.NOTIFY_PROVIDERS:
    key = "notify.by_provider." + pid
    check("%s is writable in both tiers" % key, key in hs.PREFS_WRITABLE and key in hs.WRITABLE)
    clean, errors = hs.validate_update({key: "off"}, hs.PREFS_WRITABLE)
    check("%s takes the usual switch words" % key, clean == {key: False} and not errors)
clean, errors = hs.validate_update({"notify.by_provider.mytool": False}, hs.PREFS_WRITABLE)
check("any well-formed provider id is accepted (dl_prefs tier)", clean == {"notify.by_provider.mytool": False} and not errors)
clean, errors = hs.validate_update({"notify.by_provider.mytool": False})
check("...and in the dl_settings tier", clean == {"notify.by_provider.mytool": False} and not errors)
for odd in ("notify.by_provider.", "notify.by_provider.Big", "notify.by_provider.a b", "notify.by_provider.a.b", "notify.by_provider.-x", "notify.by_provider." + "x" * 41):
    check("an odd id %r is refused" % odd, not hs.validate_update({odd: False})[0])
check("a non-switch value is refused", not hs.validate_update({"notify.by_provider.steam": "maybe"})[0])

event_path = re.compile(r"^notify\.[a-z_]+\.(enabled|level)$")      # downloads-settings.js isEventPath
check("no by_provider key looks like an event switch (the Settings page would skip it)",
      not any(event_path.match("notify.by_provider.%s" % p) for p in list(hs.NOTIFY_PROVIDERS) + ["mytool"]))
check("they are plain bools in the settings answer (a generic row)",
      all(isinstance(v, bool) for v in service.get_settings(store=Store(Path(HOME) / "v.db"))["settings"]["notify"]["by_provider"].values()))

store = Store(Path(HOME) / "w.db")
r = service.update_prefs({"notify.by_provider.steam": False, "notify.by_provider.mytool": False}, store=store)
check("dl_prefs writes it, nested", r["ok"] and store.get_settings()["notify"]["by_provider"] == {"steam": False, "playnite": True, "mytool": False})
check("...and the other notify keys are untouched", store.get_settings()["notify"]["done"] == {"enabled": True, "level": None})

# ---------------------------------------------------------------- the Dispatcher
store, sink, disp, clock = fixture()
finish(store, clock, "steam", "1", "Alpha Quest")
finish(store, clock, "playnite", "2", "Beta Quest")
run(disp, store, clock)
check("by default every provider speaks", "Alpha Quest" in titles(sink) and "Beta Quest" in titles(sink))

store, sink, disp, clock = fixture()
service.update_prefs({"notify.by_provider.steam": False}, store=store)
finish(store, clock, "steam", "1", "Alpha Quest")
finish(store, clock, "steam", "9", "Alpha Broken", state="failed")
finish(store, clock, "playnite", "2", "Beta Quest")
run(disp, store, clock)
check("a provider switched off says nothing for a finish or a failure", "Alpha" not in titles(sink))
check("...another provider still speaks", "Beta Quest" in titles(sink))
sent_before = len(sink.sent)
run(disp, store, clock, passes=3)
check("...and nothing is announced late either (the row was claimed, not held)", len(sink.sent) == sent_before)

# the queue-empty tally leaves the muted provider out
store, sink, disp, clock = fixture()
service.update_prefs({"notify.by_provider.steam": False, "notify.by_provider.playnite": False}, store=store)
finish(store, clock, "steam", "1", "Alpha Quest")
finish(store, clock, "playnite", "2", "Beta Quest")
run(disp, store, clock, passes=3)
check("with every provider muted nothing is said, not even an 'all done' tally", not sink.sent)

# the master switch still wins
store, sink, disp, clock = fixture()
service.update_prefs({"notify.providers": False}, store=store)
finish(store, clock, "steam", "1", "Alpha Quest")
finish(store, clock, "playnite", "2", "Beta Quest")
run(disp, store, clock)
check("notify.providers off still silences every provider", not sink.sent)

# switched on explicitly, an unknown id, and read on every pass
store, sink, disp, clock = fixture()
service.update_prefs({"notify.by_provider.steam": True, "notify.by_provider.mytool": False}, store=store)
finish(store, clock, "steam", "1", "Alpha Quest")
finish(store, clock, "mytool", "3", "Gamma Quest")
run(disp, store, clock)
check("switched on explicitly it speaks; a custom provider switched off does not",
      "Alpha Quest" in titles(sink) and "Gamma Quest" not in titles(sink))

store, sink, disp, clock = fixture()
service.update_prefs({"notify.by_provider.steam": False}, store=store)
finish(store, clock, "steam", "1", "Alpha Quest")
run(disp, store, clock, passes=1)
service.update_prefs({"notify.by_provider.steam": True}, store=store)
finish(store, clock, "steam", "2", "Alpha Two")
run(disp, store, clock)
check("the switch is read on every pass (off, then on: only the later finish speaks)",
      "Alpha Two" in titles(sink) and "Alpha Quest" not in titles(sink))

check("muted_providers: only an entry that is exactly False mutes",
      notify.Dispatcher.muted_providers({"by_provider": {"a": False, "b": True, "c": 0, "d": None}}) == frozenset({"a"})
      and notify.Dispatcher.muted_providers({}) == frozenset() and notify.Dispatcher.muted_providers({"by_provider": "x"}) == frozenset())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
