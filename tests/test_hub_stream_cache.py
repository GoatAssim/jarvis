"""Download Hub round 8, brief 8D (N31): `discovery.streams_for` asks all addons at once, the play intent may stop waiting,
and `stream_cache` remembers a search for 5 minutes.  Against a FAKE net with 22 fake addons (no HTTP, no network): each
fake addon answers after a fixed delay, or fails, or returns a list of streams.  The streams' wording is invented.

"All at once" is proved with a barrier that only opens when all 22 calls are in flight together (no timing).  The timing
checks use wide margins (a delay of seconds, never a tight bound).  Nothing here says how long a real search takes: the
per-addon times of a real run are not known.

Run: python tests/test_hub_stream_cache.py
"""
import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stream-cache-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.hub.stremio import addon_health, ranking, stream_cache  # noqa: E402
from jarvis.hub.stremio import addons as A  # noqa: E402
from jarvis.hub.stremio import discovery  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


def addon(i):
    manifest = {"id": "fake.%02d" % i, "name": "Addon %02d" % i, "version": "1", "types": ["series"],
                "resources": [{"name": "stream", "types": ["series"], "idPrefixes": ["tt"]}], "idPrefixes": ["tt"], "catalogs": []}
    return A.Addon("http://addon%02d.example.invalid/secret-key-%02d" % (i, i), manifest, "list")


def stream(n, quality="1080p", seeders=50):
    return {"name": "Fake\n%s" % quality, "title": "Show S02E01 %s WEB-DL DUAL AUDIO\n\U0001F464 %d" % (quality, seeders),
            "infoHash": hashlib.sha1(("torrent-%d-%s" % (n, quality)).encode()).hexdigest(),
            "behaviorHints": {"videoSize": int(1.4 * 1024 ** 3), "filename": "Show.S02E01.%s.mkv" % quality}}


class FakeNet:
    """get_json(url, label): `plan[label]` = {"delay": s, "streams": [...], "error": AddonError}.  Counts the calls."""

    def __init__(self, plan=None, barrier=None):
        self.plan = plan or {}
        self.barrier = barrier
        self.labels = []
        self.lock = threading.Lock()

    def get_json(self, url, label, **kw):
        with self.lock:
            self.labels.append(label)
        if self.barrier is not None:
            try:
                self.barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                raise A.AddonError("%s: not all calls were in flight together" % label, kind="network")
        spec = self.plan.get(label, {})
        if spec.get("delay"):
            time.sleep(spec["delay"])
        if spec.get("error") is not None:
            raise spec["error"]
        return {"streams": list(spec.get("streams", []))}

    @property
    def calls(self):
        with self.lock:
            return len(self.labels)


def ask(net, addons, vid="tt7654321:2:1", **kw):
    started = time.monotonic()
    answer = discovery.streams_for(net, addons, "series", vid, **kw)
    return answer, time.monotonic() - started


def fresh():
    stream_cache.clear()
    addon_health.reset()


WANT, _problem = ranking.normalize_want(1080, "any", None, None)

# ---- the signature and the constants
check("streams_for takes intent (default bulk) and the contract's budget_s", discovery.streams_for.__kwdefaults__.get("intent") == "bulk"
      and "budget_s" in discovery.streams_for.__kwdefaults__)
check("a separate STREAM_FAN_OUT of 24 exists and FAN_OUT (for find) is still 8", discovery.STREAM_FAN_OUT == 24 and discovery.FAN_OUT == 8)
check("the budget is still 40 s", discovery.STREAM_BUDGET_S == 40.0)

# ---- all at once: 22 calls in flight together (a barrier that would break if only 8 ran at a time)
fresh()
addons22 = [addon(i) for i in range(22)]
net = FakeNet({a.name: {"streams": [stream(i)]} for i, a in enumerate(addons22)}, barrier=threading.Barrier(22))
answer, took = ask(net, addons22)
check("22 addons are all in flight at the same time (a barrier of 22 opens)", len(answer["answered"]) == 22 and answer["failed"] == [])
check("asked is 22 and 22 streams came back", answer["asked"] == 22 and len(answer["streams"]) == 22 and net.calls == 22)

# ---- the same, by the clock, with a wide margin: 22 addons of 0.6 s each take about one delay, not three waves
fresh()
net = FakeNet({a.name: {"delay": 0.6, "streams": [stream(i)]} for i, a in enumerate(addons22)})
answer, took = ask(net, addons22)
check("22 addons of 0.6 s each finish in well under three waves (%.2f s < 1.5 s)" % took, took < 1.5 and len(answer["answered"]) == 22)

# ---- the result keeps today's shape and only adds keys
check("the answer keeps streams, answered, failed and asked", all(k in answer for k in ("streams", "answered", "failed", "asked")))
check("the added keys say: not cached, nobody skipped, nobody left waiting, complete, bulk",
      answer["cached"] is False and answer["skipped"] == [] and answer["waiting"] == [] and answer["complete"] is True and answer["intent"] == "bulk"
      and isinstance(answer["waited_s"], float))
check("every stream is a (stream dict, addon name) pair, as before", all(isinstance(s, tuple) and isinstance(s[0], dict) and s[1].startswith("Addon ") for s in answer["streams"]))

# ---- the cache: a repeated play search makes no addon call
fresh()
three = [addon(i) for i in range(3)]
plan3 = {a.name: {"streams": [stream(i), stream(10 + i)]} for i, a in enumerate(three)}
net = FakeNet(plan3)
first, _t = ask(net, three, intent="play")
check("the first play search asks all three addons and is not cached", net.calls == 3 and first["cached"] is False and len(first["streams"]) == 6)
second, _t = ask(net, three, intent="play")
check("a repeated play search makes no addon call", net.calls == 3)
check("and says so: cached True, an age, a note", second["cached"] is True and isinstance(second["cached_age_s"], int) and "no addon was asked again" in second["cache_note"])
check("the cached answer has the same streams, addons and counts", [(s["infoHash"], n) for s, n in second["streams"]] == [(s["infoHash"], n) for s, n in first["streams"]]
      and second["answered"] == first["answered"] and second["asked"] == 3)
second["streams"].clear()
second["answered"].append("changed")
third, _t = ask(net, three, intent="play")
check("changing a returned answer changes nothing in the memory", len(third["streams"]) == 6 and "changed" not in third["answered"])
other, _t = ask(net, three, vid="tt7654321:2:2", intent="play")
check("another episode is another search", net.calls == 6 and other["cached"] is False)
back, _t = ask(net, three, vid="tt7654321:2:1", intent="play")
check("a cached episode is still served after another one was asked", back["cached"] is True and net.calls == 6)

# ---- bulk never reads the cache (a plan sees live answers) but writes it (a later play search can use it)
fresh()
net = FakeNet(plan3)
a1, _t = ask(net, three)
check("a bulk search asks the addons and is not cached", net.calls == 3 and a1["cached"] is False)
a2, _t = ask(net, three)
check("a second bulk search asks them again (bulk never reads the cache)", net.calls == 6 and a2["cached"] is False)
a3, _t = ask(net, three, intent="play")
check("but a play search after a bulk one is served from what bulk got", net.calls == 6 and a3["cached"] is True)
a4, _t = ask(net, three, intent="nonsense")
check("an unknown intent behaves as bulk: asks again, says intent bulk", net.calls == 9 and a4["intent"] == "bulk" and a4["cached"] is False)

# ---- what is never remembered
fresh()
net = FakeNet({a.name: {"streams": []} for a in three})
e1, _t = ask(net, three, intent="play")
e2, _t = ask(net, three, intent="play")
check("a search that found no stream is not remembered (a release may appear later)", net.calls == 6 and e2["cached"] is False and e1["answered"])
fresh()
boom = A.AddonError("Addon x answered HTTP 500", kind="http", status=500)
net = FakeNet({a.name: {"error": boom} for a in three})
f1, _t = ask(net, three, intent="play")
f2, _t = ask(net, three, intent="play")
check("a search nobody answered is not remembered", net.calls == 6 and f2["cached"] is False and f1["answered"] == [] and len(f1["failed"]) == 3)
fresh()
check("a search with more than MAX_STREAMS streams is not kept", stream_cache.put("series", "tt1:1:1", {"streams": [({}, "a")] * (stream_cache.MAX_STREAMS + 1), "answered": ["a"]}) is False
      and stream_cache.size() == 0)
check("an answer that is not a dict, or has no streams or no answered addon, is not kept",
      not stream_cache.put("series", "tt1:1:1", None) and not stream_cache.put("series", "tt1:1:1", {"streams": [], "answered": ["a"]})
      and not stream_cache.put("series", "tt1:1:1", {"streams": [({}, "a")], "answered": []}))

# ---- the lifetime: 5 minutes, with an injected clock
fresh()
clock = [1000.0]
real_clock = stream_cache._clock
stream_cache._clock = lambda: clock[0]
try:
    check("the lifetime is 5 minutes", stream_cache.TTL_S == 300.0)
    answer_ok = {"streams": [({"infoHash": "x"}, "a")], "answered": ["a"], "failed": [], "asked": 1}
    stream_cache.put("series", "tt1:1:1", answer_ok)
    clock[0] = 1299.0
    check("299 s later it is still there, and its age is 299 s", (stream_cache.get("series", "tt1:1:1") or {}).get("cached_age_s") == 299)
    clock[0] = 1301.0
    check("301 s later it is gone", stream_cache.get("series", "tt1:1:1") is None and stream_cache.size() == 0)
    clock[0] = 2000.0
    stream_cache.put("series", "tt1:1:1", answer_ok)
    clock[0] = 1500.0
    check("a clock that went backwards drops the entry instead of serving it for ever", stream_cache.get("series", "tt1:1:1") is None)

    # bounded
    clock[0] = 3000.0
    for n in range(stream_cache.MAX_ENTRIES + 6):
        stream_cache.put("series", "tt1:1:%d" % n, answer_ok)
    check("the memory is bounded to MAX_ENTRIES searches, oldest first",
          stream_cache.size() == stream_cache.MAX_ENTRIES and stream_cache.get("series", "tt1:1:0") is None
          and stream_cache.get("series", "tt1:1:%d" % (stream_cache.MAX_ENTRIES + 5)) is not None)
    # partial entries
    stream_cache.clear()
    stream_cache.put("series", "tt2:1:1", answer_ok, complete=False)
    check("a partial entry is served to a caller that accepts it, marked complete False", (stream_cache.get("series", "tt2:1:1") or {}).get("complete") is False)
    check("a caller that needs a complete answer gets None for a partial one", stream_cache.get("series", "tt2:1:1", complete_only=True) is None)
    stream_cache.put("series", "tt2:1:2", answer_ok, complete=True)
    check("a complete entry is served to both", stream_cache.get("series", "tt2:1:2", complete_only=True)["complete"] is True)
    check("clear() says how many it forgot", stream_cache.clear() == 2 and stream_cache.size() == 0)
finally:
    stream_cache._clock = real_clock

# ---- the early return (play only, and only with a want)
discovery_min = discovery.EARLY_MIN_S
discovery.EARLY_MIN_S = 0.3                      # the real value is 6 s; shortened so the test is quick
try:
    def slow_set(n_fast=3, exact_each=3, slow_delay=3.0, quality="1080p", seeders=50):
        addons = [addon(i) for i in range(n_fast + 1)]
        plan = {}
        for i in range(n_fast):
            plan[addons[i].name] = {"streams": [stream(100 * i + k, quality, seeders) for k in range(exact_each)]}
        plan[addons[n_fast].name] = {"delay": slow_delay, "streams": [stream(900, quality, seeders)]}
        return addons, FakeNet(plan)

    fresh()
    addons, net = slow_set()
    answer, took = ask(net, addons, intent="play", want=WANT)
    check("play with a want: 3 addons gave 9 exact torrents with seeders, so it stops waiting for the slow one (%.2f s < 2.5 s)" % took,
          took < 2.5 and len(answer["answered"]) == 3 and answer["waiting"] == ["Addon 03"])
    check("the straggler is named in failed as 'still waiting'", any(s.startswith("still waiting:") and "Addon 03" in s for s in answer["failed"]))
    check("the answer says it is not complete, and the others' streams are all there", answer["complete"] is False and len(answer["streams"]) == 9)
    check("a dropped straggler is not a failure that counts: its health is clean", addon_health.state(addons[3])["fails"] == 0)
    again, _t = ask(net, addons, intent="play", want=WANT)
    check("the partial answer is remembered for play: a repeat asks nobody", again["cached"] is True and again["complete"] is False and net.calls == 4)

    fresh()
    addons, net = slow_set()
    answer, took = ask(net, addons, intent="bulk", want=WANT)
    check("the same set in bulk waits for the slow addon (%.2f s >= 2.8 s) and names no 'still waiting'" % took,
          took >= 2.8 and len(answer["answered"]) == 4 and answer["waiting"] == [] and not any("still waiting" in s for s in answer["failed"]))
    check("a complete bulk answer is complete and was remembered for a later play search", answer["complete"] is True and stream_cache.get("series", "tt7654321:2:1", complete_only=True) is not None)

    fresh()
    addons, net = slow_set(slow_delay=2.0)
    answer, took = ask(net, addons, intent="play")
    check("play without a want has no idea what 'exact' is, so it waits for everyone (%.2f s >= 1.8 s)" % took, took >= 1.8 and len(answer["answered"]) == 4)

    fresh()
    addons, net = slow_set(exact_each=1, slow_delay=1.5)
    answer, took = ask(net, addons, intent="play", want=WANT)
    check("exactly 3 exact torrents from 3 addons is enough: it stops waiting for the slow one (%.2f s < 1.2 s)" % took,
          took < 1.2 and answer["waiting"] == ["Addon 03"])
    fresh()
    two = [addon(i) for i in range(3)]
    net2 = FakeNet({two[0].name: {"streams": [stream(1)]}, two[1].name: {"streams": [stream(2)]}, two[2].name: {"delay": 1.5, "streams": [stream(3)]}})
    answer, took = ask(net2, two, intent="play", want=WANT)
    check("2 exact torrents from 2 addons are not enough: it waits for the third (%.2f s >= 1.4 s)" % took, took >= 1.4 and len(answer["answered"]) == 3)

    fresh()
    one = [addon(i) for i in range(3)]
    net3 = FakeNet({one[0].name: {"streams": [stream(k) for k in range(8)]}, one[1].name: {"delay": 1.5, "streams": [stream(50)]},
                    one[2].name: {"delay": 1.5, "streams": [stream(60)]}})
    answer, took = ask(net3, one, intent="play", want=WANT)
    check("8 exact torrents from ONE addon are not enough: two addons must have answered (%.2f s >= 1.4 s)" % took, took >= 1.4 and len(answer["answered"]) == 3)

    fresh()
    addons, net = slow_set(slow_delay=1.5, seeders=0)
    answer, took = ask(net, addons, intent="play", want=WANT)
    check("exact torrents nobody seeds do not count: it waits (%.2f s >= 1.4 s)" % took, took >= 1.4 and len(answer["answered"]) == 4)

    fresh()
    addons, net = slow_set(slow_delay=1.5, quality="720p")
    answer, took = ask(net, addons, intent="play", want=WANT)
    check("720p releases are not exact for a 1080p want: it waits (%.2f s >= 1.4 s)" % took, took >= 1.4 and len(answer["answered"]) == 4)

    # the minimum time: enough is in at once, but it still waits for EARLY_MIN_S
    discovery.EARLY_MIN_S = 1.0
    fresh()
    addons, net = slow_set(slow_delay=4.0)
    answer, took = ask(net, addons, intent="play", want=WANT)
    check("it waits at least the minimum time even when enough is in at once (%.2f s, between 0.9 and 3.5)" % took,
          0.9 <= took < 3.5 and answer["waiting"] == ["Addon 03"])
finally:
    discovery.EARLY_MIN_S = discovery_min

check("the real thresholds are the ones of N31a: 2 addons, 3 exact torrents, 6 s", discovery.EARLY_MIN_ADDONS == 2 and discovery.EARLY_MIN_EXACT == 3 and discovery.EARLY_MIN_S == 6.0)

# ---- the budget still cuts a hopeless wait (bulk), and says "too slow" as before
fresh()
addons = [addon(0), addon(1)]
net = FakeNet({addons[0].name: {"streams": [stream(1)]}, addons[1].name: {"delay": 5.0, "streams": [stream(2)]}})
answer, took = ask(net, addons, budget_s=0.6)
check("a budget of 0.6 s drops the slow addon as 'was too slow' (today's sentence) and marks the answer partial",
      took < 3.0 and "Addon 01 was too slow" in answer["failed"] and answer["complete"] is False and answer["answered"] == ["Addon 00"])

# ---- no stream-capable addon at all: today's sentence, nothing cached
fresh()
none = A.Addon("http://meta.example.invalid/x", {"name": "Meta only", "types": ["series"], "resources": ["meta"]}, "list")
answer, _t = ask(FakeNet(), [none])
check("no addon offers streams: today's sentence, asked 0", answer["asked"] == 0 and "no installed addon offers streams" in answer["failed"][0])

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
