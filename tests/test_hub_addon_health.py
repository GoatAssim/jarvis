"""Download Hub round 8, brief 8D (N31b): `addon_health` and how `discovery.streams_for` uses it.

Part 1 is against a FAKE net with injected errors and an INJECTED CLOCK (the six hours are not waited for).  Part 2 uses
the loopback fake addon pair of tests/_hub_fake_addon.py, which really answers 404, 500, hangs, or is refused by the SSRF
guard.  No network.  The addons' wording is invented; nothing here ran against a real addon.

Run: python tests/test_hub_addon_health.py
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-addon-health-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_addon as fake  # noqa: E402
from jarvis.hub.stremio import addon_health as H  # noqa: E402
from jarvis.hub.stremio import addons as A  # noqa: E402
from jarvis.hub.stremio import discovery, stream_cache  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


T = [1_000_000.0]
H._clock = lambda: T[0]
HOUR = 3600.0


def addon(i, name=None):
    manifest = {"id": "fake.%02d" % i, "name": name or "Addon %02d" % i, "version": "1", "types": ["series"],
                "resources": [{"name": "stream", "types": ["series"], "idPrefixes": ["tt"]}], "idPrefixes": ["tt"], "catalogs": []}
    return A.Addon("http://addon%02d.example.invalid/secret-key-%02d" % (i, i), manifest, "list")


def stream(n):
    import hashlib
    return {"name": "Fake\n1080p", "title": "Show S02E01 1080p WEB-DL DUAL AUDIO\n\U0001F464 50",
            "infoHash": hashlib.sha1(("t%d" % n).encode()).hexdigest(), "behaviorHints": {"videoSize": 1500000000}}


def err404(label="x"):
    return A.AddonError("%s answered HTTP 404" % label, kind="http", status=404)


class FakeNet:
    def __init__(self, plan):
        self.plan = plan
        self.labels = []
        self.lock = threading.Lock()

    def get_json(self, url, label, **kw):
        with self.lock:
            self.labels.append(label)
        spec = self.plan.get(label, {})
        if spec.get("raise") is not None:
            raise spec["raise"]
        return {"streams": [stream(1)]}

    def asked(self, label):
        with self.lock:
            return self.labels.count(label)


def ask(net, addons, **kw):
    stream_cache.clear()
    return discovery.streams_for(net, addons, "series", "tt7654321:2:1", **kw)


# ---- the numbers and the classification of failures
check("3 failures in a row, 6 hours", H.FAILS_TO_SKIP == 3 and H.SKIP_S == 6 * HOUR)
E = A.AddonError
check("HTTP 404, 410, 500, 503 and 403 count", all(H.counts_as_failure(E("x", kind="http", status=s)) for s in (404, 410, 500, 503, 403)))
check("HTTP 408 and 429 (try later) do not count", not H.counts_as_failure(E("x", kind="http", status=429)) and not H.counts_as_failure(E("x", kind="http", status=408)))
check("an HTTP error with no status does not count", not H.counts_as_failure(E("x", kind="http")))
check("a timeout / refused connection / unresolved name (kind network) counts and so does another guard refusal",
      H.counts_as_failure(E("x", kind="network")) and H.counts_as_failure(E("x", kind="refused")))
check("a private or local address refused by the guard does not count, and is recognised", not H.counts_as_failure(E("x", kind="local_refused"))
      and H.is_local_refusal(E("x", kind="local_refused")) and not H.is_local_refusal(E("x", kind="refused")))
check("bad JSON, a too-big reply, a redirect loop, an error without a kind and a plain exception do not count",
      not any(H.counts_as_failure(x) for x in (E("x", kind="json"), E("x", kind="size"), E("x", kind="redirect"), E("x"), ValueError("x"))))
check("AddonError(message) alone still works as before", str(A.AddonError("plain")) == "plain" and A.AddonError("plain").kind is None)

# ---- three failures skip; a success clears; six hours; the same addon asked again after them
H.reset()
good, bad = addon(0), addon(1)
net = FakeNet({bad.name: {"raise": err404(bad.name)}})
for n in (1, 2):
    a = ask(net, [good, bad])
    check("search %d: the failing addon is asked and its failure is said" % n, net.asked(bad.name) == n and any("HTTP 404" in s for s in a["failed"]) and a["skipped"] == [])
    check("search %d: %d failure(s) in a row remembered, not skipped yet" % (n, n), H.state(bad)["fails"] == n and not H.state(bad)["skipped"])
a = ask(net, [good, bad])
check("search 3: the third failure is said and now it is skipped for 6 hours", net.asked(bad.name) == 3 and H.state(bad)["skipped"] and H.state(bad)["until"] == T[0] + 6 * HOUR)
a = ask(net, [good, bad])
check("search 4: the addon is not asked", net.asked(bad.name) == 3 and net.asked(good.name) == 4 and a["asked"] == 1)
check("search 4: it is named once in failed, as 'skipped: failed 3 times'", sum(1 for s in a["failed"] if "skipped: failed 3 times" in s) == 1
      and any("Addon 01" in s for s in a["failed"]) and a["skipped"] == ["Addon 01"])
check("search 4: the answer of the other addon is complete", a["answered"] == ["Addon 00"] and len(a["streams"]) == 1 and a["complete"] is True)
check("a skipped addon is not hidden: it is in `failed` at every search, not only the first", any("skipped" in s for s in ask(net, [good, bad])["failed"]))
T[0] += 6 * HOUR - 60
a = ask(net, [good, bad])
check("5h59m later it is still skipped", net.asked(bad.name) == 3 and a["skipped"] == ["Addon 01"])
T[0] += 120
a = ask(net, [good, bad])
check("after the six hours it is asked again, once", net.asked(bad.name) == 4 and a["skipped"] == [])
check("and a failure now skips it again straight away (the count was kept at 3)", H.state(bad)["fails"] == 4 and H.state(bad)["skipped"])
T[0] += 7 * HOUR
net.plan[bad.name] = {}
a = ask(net, [good, bad])
check("when it answers after the six hours its count is cleared and it is not skipped", net.asked(bad.name) == 5 and H.state(bad)["fails"] == 0
      and not H.state(bad)["skipped"] and "Addon 01" in a["answered"])
a = ask(net, [good, bad])
check("it is then asked as normal", net.asked(bad.name) == 6 and a["skipped"] == [])

# ---- 'in a row': a success in between clears the count
H.reset()
flaky = addon(2)
net = FakeNet({flaky.name: {"raise": err404(flaky.name)}})
ask(net, [good, flaky])
ask(net, [good, flaky])
net.plan[flaky.name] = {}
ask(net, [good, flaky])
check("two failures and then an answer: the count is back to 0", H.state(flaky)["fails"] == 0)
net.plan[flaky.name] = {"raise": err404(flaky.name)}
ask(net, [good, flaky])
ask(net, [good, flaky])
check("two more failures after that are still only 2 in a row: not skipped", H.state(flaky)["fails"] == 2 and not H.state(flaky)["skipped"])

# ---- a timeout counts, a stray exception does not, a dropped straggler does not
H.reset()
slow = addon(3)
net = FakeNet({slow.name: {"raise": A.AddonError("Addon 03 didn't answer (ReadTimeout)", kind="network")}})
for _ in range(3):
    ask(net, [good, slow])
check("three timeouts skip an addon", H.state(slow)["skipped"])
H.reset()
net = FakeNet({slow.name: {"raise": ValueError("odd")}})
a = ask(net, [good, slow])
check("an unexpected exception from one addon is said ('failed (ValueError)'), loses nothing else, and is not counted",
      any("Addon 03 failed (ValueError)" in s for s in a["failed"]) and a["answered"] == ["Addon 00"] and H.state(slow)["fails"] == 0)

# ---- a search nobody answered says nothing about the addons (the PC may be offline)
H.reset()
a1, a2 = addon(4), addon(5)
net = FakeNet({a1.name: {"raise": err404()}, a2.name: {"raise": err404()}})
for _ in range(4):
    ask(net, [a1, a2])
check("four searches in which every addon failed count nothing (an outage must not skip every addon for six hours)",
      H.state(a1)["fails"] == 0 and H.state(a2)["fails"] == 0 and net.asked(a1.name) == 4)

# ---- a skip never leaves a search with nobody to ask
H.reset()
for _ in range(3):
    H.record(failed=[a1, a2])
check("both addons are skipped right now", H.state(a1)["skipped"] and H.state(a2)["skipped"])
net = FakeNet({})
a = ask(net, [a1, a2])
check("a search whose every target is skipped asks them all anyway", net.asked(a1.name) == 1 and net.asked(a2.name) == 1
      and sorted(a["answered"]) == ["Addon 04", "Addon 05"])
check("and then they answered, so their counts are cleared", H.state(a1)["fails"] == 0 and H.state(a2)["fails"] == 0 and a["skipped"] == [])

# ---- a private or local address refused by the guard: named once, never counted
H.reset()
l1, l2 = addon(6, "Local One"), addon(7, "Local Two")
refusal = A.AddonError("x was refused: host points at a private or local address (127.0.0.1); refused. If that is intended, add it to allow_private_hosts with dl_settings.",
                       kind="local_refused")
net = FakeNet({l1.name: {"raise": refusal}, l2.name: {"raise": refusal}})
for _ in range(5):
    a = ask(net, [good, l1, l2])
local_sentences = [s for s in a["failed"] if "allow_private_hosts" in s]
check("two refused local addons are named in ONE sentence, with the sentence the code uses", len(local_sentences) == 1
      and "refused: add it to allow_private_hosts if intended" in local_sentences[0] and "Local One" in local_sentences[0] and "Local Two" in local_sentences[0])
check("the long guard sentence is not repeated per addon", not any("points at a private" in s for s in a["failed"]))
check("five searches later they are still asked (and refused) and never skipped: not a failure that counts",
      H.state(l1)["fails"] == 0 and not H.state(l1)["skipped"] and net.asked(l1.name) == 5 and a["skipped"] == [])

# ---- the file: what is in it, and what is not
H.reset()
net = FakeNet({bad.name: {"raise": err404()}})
ask(net, [good, bad])
raw = H.path().read_text(encoding="utf-8")
data = json.loads(raw)
check("the memory is one small JSON file in the hub's data folder", H.path().name == "addon_health.json" and H.path().parent == Path(HOME) / ".jarvis" / "hub")
check("it holds a short hash per addon, counts and times, and never an address, a name or a key",
      list(data["addons"]) == [H.key_of(bad)] and re.fullmatch(r"[0-9a-f]{16}", H.key_of(bad)) and "secret-key" not in raw and "example.invalid" not in raw and "Addon" not in raw)
check("the hash differs per address", H.key_of(good) != H.key_of(bad) and H.key_of(bad) == H.key_of(addon(1)))
check("no temp file is left behind", not list(H.path().parent.glob("*.tmp")))

# ---- a damaged file is an empty memory, and is repaired by the next write
for damaged in ("{not json", "[]", "null", '{"addons": []}', '{"addons": {"k": 5}}', "", "\x00\x01\x02"):
    H.path().write_text(damaged, encoding="utf-8")
    active, skipped = H.split([good, bad])
    check("a damaged file (%r) gives an empty memory without raising" % damaged[:12], active == [good, bad] and skipped == [])
H.path().write_text(json.dumps({"addons": {"a": {"fails": "x"}, "b": {"fails": -1}, "c": {"fails": True}, "d": {"fails": 5, "until": 9e18},
                                           H.key_of(bad): {"fails": 7, "until": T[0] + 100, "last": T[0]}}}), encoding="utf-8")
check("bad entries are ignored one by one, a good one survives", H.state(bad)["fails"] == 7 and H.state(bad)["skipped"])
H.path().write_text('{"addons": {"%s": {"fails": 3, "until": NaN, "last": NaN}}}' % H.key_of(bad), encoding="utf-8")
check("a NaN time is read as 0 (not skipped), not as a crash", H.state(bad)["fails"] == 3 and not H.state(bad)["skipped"])
H.path().write_bytes(b"\xff\xfe\x00bad bytes")
H.record(failed=[good])
check("the next write repairs a damaged file", json.loads(H.path().read_text(encoding="utf-8"))["addons"][H.key_of(good)]["fails"] == 1)

# ---- an unwritable data folder: still no exception, and nothing skipped
real_home = os.environ["HOME"]
blocked = tempfile.mkdtemp(prefix="jarvis-health-blocked-")
Path(blocked, ".jarvis").write_text("a file where the folder should be", encoding="utf-8")
os.environ["HOME"] = os.environ["USERPROFILE"] = blocked
try:
    raised = False
    try:
        H.record(ok=[good], failed=[bad])
        H.record(failed=[bad])
        H.record(failed=[bad])
        active, skipped = H.split([good, bad])
        H.reset()
    except Exception:  # noqa: BLE001
        raised = True
    check("a data folder that cannot be written never raises, and nothing is skipped without a memory", not raised and skipped == [])
finally:
    os.environ["HOME"] = os.environ["USERPROFILE"] = real_home

# ---- reset()
H.reset()
H.record(failed=[good, bad])
H.record(failed=[bad])
check("reset() says how many addons it forgot, and the file is gone", H.reset() == 2 and not H.path().exists())
check("reset() on nothing is 0 and does not raise", H.reset() == 0)
H.record(failed=[bad])
H.record(failed=[bad])
H.record(failed=[bad])
check("after reset the count starts from zero again (3 more failures skip)", H.state(bad)["skipped"])
H.path().write_text("{broken", encoding="utf-8")
check("reset() on a damaged file is 0 and removes it", H.reset() == 0 and not H.path().exists())

# ---- old and many entries are dropped
H.reset()
H.record(failed=[good], now=T[0] - 40 * 24 * HOUR)
H.record(failed=[bad])
check("an entry nobody touched for 30 days is dropped at the next write", H.state(good)["fails"] == 0 and H.state(bad)["fails"] == 1)


class Thing:
    def __init__(self, n):
        self.base = "http://thing%d.example.invalid" % n


H.reset()
H.record(failed=[Thing(n) for n in range(H.MAX_ENTRIES + 25)])
check("the memory is bounded to MAX_ENTRIES addons", len(json.loads(H.path().read_text(encoding="utf-8"))["addons"]) == H.MAX_ENTRIES)

# ---- the sentences
check("skipped sentence", H.skipped_sentence(["B", "A", "A"]) == "skipped: failed 3 times in a row, not asked again for 6 h: A, B")
check("local sentence", H.local_refused_sentence(["B", "A"]) == "refused: add it to allow_private_hosts if intended: A, B")
check("a long list of names is cut with a count", H.skipped_sentence(["n%d" % i for i in range(9)]).endswith("and 3 more"))

# ---- part 2: the real loopback fake addons (404, 500, a hang, the SSRF guard)
H._clock = time.time
H.reset()
BASE, ST, SERVER = fake.start()
admin = A.Net(allow=["127.0.0.1"])
cat = A.Addon(BASE + "/cat", admin.get_json(BASE + "/cat/manifest.json", "a manifest"), "list")
strm = A.Addon(BASE + "/str", admin.get_json(BASE + "/str/manifest.json", "a manifest"), "list")
VID = fake.SHOW + ":2:1"


def real_ask(net=None):
    stream_cache.clear()
    return discovery.streams_for(net or admin, [cat, strm], "series", VID)


for mode, word in ((404, "HTTP 404"), (500, "HTTP 500"), ("timeout", "didn't answer")):
    H.reset()
    ST.__init__()
    ST.fail_mode["/str"] = mode
    ST.hang_s = 0.3
    first = real_ask()
    check("%s: the fake addon's failure is reported (%r) and the other addon still answers" % (mode, word),
          any(word in s for s in first["failed"]) and first["answered"] == ["Fake Catalog"] and len(first["streams"]) > 0)
    real_ask()
    real_ask()
    check("%s: after three of them the addon is skipped" % mode, H.state(strm)["skipped"])
    before = ST.stream_calls.get("/str", 0)
    fourth = real_ask()
    check("%s: the fourth search makes no call to it (the fake addon's own counter) and names it once" % mode,
          ST.stream_calls.get("/str", 0) == before == 3 and fourth["skipped"] == ["Fake Streams"]
          and sum(1 for s in fourth["failed"] if "skipped: failed 3 times" in s) == 1 and ST.stream_calls.get("/cat", 0) == 4)

H.reset()
ST.__init__()
guard = A.Net(allow=[])                              # the owner has not allow-listed the loopback address
refused = real_ask(guard)
check("the real SSRF guard: both loopback addons are refused with no call reaching them", sum(ST.calls.values()) == 0 and refused["answered"] == [])
sentences = [s for s in refused["failed"] if "allow_private_hosts" in s]
check("both are named once, in the allow_private_hosts sentence, and the guard's long sentence is gone",
      len(sentences) == 1 and "Fake Catalog" in sentences[0] and "Fake Streams" in sentences[0] and len(refused["failed"]) == 1)
for _ in range(4):
    real_ask(guard)
check("refused local addons are never counted or skipped, however often", H.state(cat)["fails"] == 0 and H.state(strm)["fails"] == 0)
check("the sentence does not carry an address, a path or a key", "127.0.0.1" not in sentences[0] and "/cat" not in sentences[0] and BASE not in sentences[0])

SERVER.shutdown()
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
