"""Stremio follow-ups from the 2026-10-25 Rick and Morty run: nine episodes shared two dead season packs and each one
waited the full 90 s on them; the fallback chosen for E11 was an 840 MB BDRip next to 350 MB releases; a season's
fallbacks were the same torrent again.  No network.

Run: python tests/test_hub_stremio_followup.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stremio-followup-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub.engines import stremio as seng  # noqa: E402
from jarvis.hub.stremio import ranking  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


GB = 1024 ** 3
WANT = ranking.normalize_want(None, None, None, None)[0]


def rel(infohash, size_gb, seeders, idx=1):
    return {"resolution": 1080, "audio": "unknown", "codec": None, "source": None, "size": int(size_gb * GB), "seeders": seeders,
            "banned": None, "binge": None, "torrent": True, "direct": False, "infohash": infohash, "file_idx": idx,
            "name": "r-" + infohash[:4], "stream": {}}


H = lambda c: c * 40  # noqa: E731

# ---- size
releases = [rel(H("a"), 0.35, 600), rel(H("b"), 0.34, 400), rel(H("c"), 0.36, 300), rel(H("d"), 0.84, 1500)]
kept, _ = ranking.rank(releases, WANT)
check("an 840 MB release with the most seeders no longer beats three 350 MB ones", kept[0]["infohash"] != H("d"))
check("... but it is still offered (last), not thrown away", kept[-1]["infohash"] == H("d"))
kept2, _ = ranking.rank([rel(H("a"), 0.35, 100), rel(H("b"), 0.34, 90)], WANT)
check("with fewer than three sizes to compare nothing is penalised", [c["infohash"] for c in kept2] == [H("a"), H("b")])

# ---- fallbacks are other torrents
many = [rel(H("a"), 0.35, 600, idx=3), rel(H("a"), 0.35, 590, idx=4), rel(H("b"), 0.34, 400), rel(H("c"), 0.36, 300),
        rel(H("d"), 0.37, 200), rel(H("e"), 0.38, 150), rel(H("f"), 0.39, 100)]
kept3, _ = ranking.rank(many, WANT)
answer = ranking.choose(kept3, "ask")
hashes = [answer["pick"]["infohash"]] + [f["infohash"] for f in answer["fallbacks"]]
check("fallbacks never repeat the pick's torrent or each other", len(hashes) == len(set(hashes)))
check("up to four fallbacks are kept", len(answer["fallbacks"]) == 4 and ranking.MAX_FALLBACKS == 4)

# ---- the dead-torrent memory
check("a torrent nobody has failed on is not dead", not seng._is_dead(H("1")))
seng._mark_dead(H("1"))
check("a torrent that gave no file list is remembered as dead", seng._is_dead(H("1")) and seng._is_dead(H("1").upper()))
seng._forget_dead(H("1"))
check("... and forgotten again once it answers", not seng._is_dead(H("1")))
seng._mark_dead(H("2"))
seng._dead[H("2")] = time.monotonic() - seng.DEAD_TTL_S - 5
check("a memory older than half an hour is dropped", not seng._is_dead(H("2")))


class Ctx:
    def __init__(self):
        self.events, self.updates = [], []

    def update(self, **kw):
        self.updates.append(kw)
        return True

    def event(self, level, text):
        self.events.append(text)


def mag(h):
    return "magnet:?xt=urn:btih:%s" % h


seng._mark_dead(H("a"))
ref = {"fallbacks": [mag(H("a")), mag(H("b")), mag(H("c"))]}
ctx = Ctx()
out = seng._switch_candidate(ctx, {}, ref, "127.0.0.1:11470", "no seeders")
active = [u for u in ctx.updates if u.get("source_ref")][-1]["source_ref"]
check("the switch skips a release whose torrent is dead and takes the next live one", active["active"]["infohash"] == H("b"))
check("... and says it skipped one", any("skipped" in e for e in ctx.events))
check("... the dead release is dropped from the list, the live one after it stays", [f for f in active["fallbacks"]] == [mag(H("c"))])

seng._mark_dead(H("b"))
seng._mark_dead(H("c"))
ctx2 = Ctx()
ref2 = {"fallbacks": [mag(H("a")), mag(H("b"))]}
out2 = seng._switch_candidate(ctx2, {}, ref2, "127.0.0.1:11470", "no seeders")
active2 = [u for u in ctx2.updates if u.get("source_ref")][-1]["source_ref"]
check("when every release left is dead it still tries the first, never gives up early", out2 is not None and active2["active"]["infohash"] == H("a"))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
