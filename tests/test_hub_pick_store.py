"""Brief 8C (item N26, contract C1): the short memory of the last stream search.

The candidates are built by the real ranking code (parse_release + rank) from hand-made Stremio stream objects
(invented wording, like _hub_fake_addon.py's).  Nothing here ran against a real addon.  The clock is injected
(pick_store._now); the mirror file lives in a temporary HOME.  No network.

Run: python tests/test_hub_pick_store.py
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-pick-store-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import paths  # noqa: E402
from jarvis.hub.stremio import links, pick_store as ps, ranking  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


FILE = paths.hub_dir() / ps.FILE_NAME
CLOCK = [5_000_000.0]
ps._now = lambda: CLOCK[0]


def reset():
    CLOCK[0] = 5_000_000.0
    ps.clear()


def h(n):
    return ("%040x" % n)


def stream(n, text="Show S01E03 1080p WEB-DL DUAL AUDIO", idx=None, url=None, seeders=10 + 0, **extra):
    out = {"name": "Fake\n1080p", "title": "%s #%d\n\U0001F464 %d" % (text, n, seeders), "infoHash": h(n),
           "sources": ["tracker:udp://tracker.example.invalid:1337/announce", "dht:" + h(n)],
           "behaviorHints": {"filename": "Show.S01E03.%d.mkv" % n, "videoSize": 1_500_000_000 + n, "bingeGroup": "grp"}}
    if idx is not None:
        out["fileIdx"] = idx
    if url:
        out["url"] = url
    out.update(extra)
    return out


def ranked(streams, quality=1080, audio="dub"):
    want, problem = ranking.normalize_want(quality, audio, None, None)
    assert problem is None
    releases = []
    for s in streams:
        release = ranking.parse_release(s)
        release["addon"] = "Fake Addon"
        releases.append(release)
    kept, _ = ranking.rank(releases, want, torrents_only=False)
    return kept


VID = "tt2861424:1:3"

# =========================================================================== remember and recall
reset()
cands = ranked([stream(1, idx=4), stream(2), stream(3, text="Show S01E03 720p Eng Sub")])
check("remember keeps a ranked list", ps.remember(VID, cands) is True)
for number, c in enumerate(cands, 1):
    got = ps.recall(VID, number)
    check("recall rank %d is the candidate at that position" % number, got is not None and got["infohash"] == c["infohash"] and got["label"] == c["label"])
first = next(ps.recall(VID, n) for n in (1, 2, 3) if ps.recall(VID, n)["infohash"] == h(1))      # the stream that carries fileIdx 4
check("the contract's fields are there: stream, infohash, file_idx, name, label, tier, size, seeders, addon",
      all(k in first for k in ("stream", "infohash", "file_idx", "name", "label", "tier", "size", "seeders", "addon")))
check("the file index the addon gave is kept (the point of N26)", first["file_idx"] == 4 and first["stream"]["fileIdx"] == 4)
check("the slim stream still parses as a torrent stream for play_episode", links.parse_stream(first["stream"])["infohash"] == first["infohash"]
      and links.parse_stream(first["stream"])["file_idx"] == 4)
check("trackers survive, so the server can find peers", first["stream"]["sources"] and first["stream"]["sources"][0].startswith("tracker:udp://tracker.example.invalid"))
check("a rank that does not exist gives None", ps.recall(VID, 4) is None and ps.recall(VID, 0) is None and ps.recall(VID, -1) is None
      and ps.recall(VID, 10 ** 6) is None)
check("a rank that is not a whole number gives None, not an error", ps.recall(VID, "x") is None and ps.recall(VID, None) is None
      and ps.recall(VID, True) is None and ps.recall(VID, []) is None)
check("a rank given as the text \"2\" works (a model may send it that way)", ps.recall(VID, "2")["infohash"] == cands[1]["infohash"])
check("another video id has nothing", ps.recall("tt2861424:1:4", 1) is None and ps.recall("tt7654321", 1) is None)
check("a bad video id is refused and recalls nothing", ps.remember("a b", cands) is False and ps.recall("../x", 1) is None
      and ps.remember(None, cands) is False and ps.recall(None, 1) is None)

before = ps.recall(VID, 1)
got = ps.recall(VID, 1)
got["infohash"] = "changed"
got["stream"]["sources"].append("tracker:udp://evil:1/x")
check("recall hands out a copy: changing it changes nothing stored", ps.recall(VID, 1) == before and ps.recall(VID, 1)["infohash"] != "changed"
      and "tracker:udp://evil:1/x" not in ps.recall(VID, 1)["stream"]["sources"])

# the last search wins
ps.remember(VID, cands[:1])
check("a new search for the same video id replaces the old one", ps.recall(VID, 1) is not None and ps.recall(VID, 2) is None)
ps.remember(VID, [])
check("an empty search is remembered too: the last search found nothing", ps.recall(VID, 1) is None)
ps.remember(VID, cands)

# =========================================================================== what is kept, and what never is
reset()
secret = "https://cdn.example.invalid/x/secret-token.mkv"
direct = {"name": "Direct", "title": "Show S01E03 1080p DUB", "url": secret, "behaviorHints": {"proxyHeaders": {"request": {"Authorization": "SECRETKEY"}}}}
with_url = stream(5, url=secret, extra_key="SECRETKEY", externalUrl="https://x.invalid/SECRETKEY")
cs = ranked([with_url, direct])
ps.remember("tt0000001", cs)
dump = json.dumps([ps.recall("tt0000001", n) for n in (1, 2)]) + FILE.read_text(encoding="utf-8")
check("no stream address and no secret from a stream is kept, in memory or in the file", "secret-token" not in dump and "SECRETKEY" not in dump
      and "externalUrl" not in dump and "proxyHeaders" not in dump)
direct_row = next(ps.recall("tt0000001", n) for n in (1, 2) if not ps.recall("tt0000001", n)["torrent"])
check("a direct-link candidate is kept without its address and is marked as not a torrent", direct_row["torrent"] is False
      and "url" not in direct_row["stream"] and direct_row["infohash"] is None)
check("the infohash is a clean 40-hex string and the file index may be missing", ps.recall("tt0000001", 1)["infohash"] in (h(5), None))

weird = [{"infohash": h(9), "label": "x", "size": float("inf"), "seeders": float("nan"), "name": "n\x00\x1b[31mn" + "z" * 900,
          "stream": {"infoHash": h(9), "fileIdx": -3, "sources": ["tracker:http://a b/x", "tracker:udp://ok.example.invalid:1/a", 5, None]},
          "extra": {"nested": object()}, "languages": ["english", 3, "x" * 99]}, "not a dict", None, 7]
check("junk in the list is skipped and junk values are cleaned", ps.remember("tt0000002", weird) is True)
w = ps.recall("tt0000002", 1)
check("an infinite or NaN number becomes null, control characters and long text are cut", w["size"] is None and w["seeders"] is None
      and "\x00" not in w["name"] and "\x1b" not in w["name"] and len(w["name"]) <= 200 and "extra" not in w)
check("a bad file index is dropped, a tracker with a space is dropped, a good one stays", "fileIdx" not in w["stream"]
      and w["stream"]["sources"] == ["tracker:udp://ok.example.invalid:1/a"])
check("languages are kept as short strings (a number is dropped)", w["languages"][0] == "english" and len(w["languages"]) == 2 and len(w["languages"][1]) <= 20)
check("a non-dict entry does not shift the ranks of the others", ps.recall("tt0000002", 2) is None)

# =========================================================================== expiry
reset()
ps.remember(VID, cands)
CLOCK[0] += ps.TTL_S - 1
check("a search is still there just inside 600 s", ps.recall(VID, 1) is not None)
CLOCK[0] += 2
check("...and gone after it (memory and file both)", ps.recall(VID, 1) is None)
ps.remember(VID, cands)
CLOCK[0] += ps.TTL_S + 1
ps.remember("tt0000003", cands)
check("an expired search is pruned the next time something is remembered", ps.recall(VID, 1) is None and ps.recall("tt0000003", 1) is not None
      and VID not in json.loads(FILE.read_text(encoding="utf-8"))["searches"])

# =========================================================================== the bounds
reset()
for n in range(ps.MAX_SEARCHES + 4):
    CLOCK[0] += 1
    ps.remember("tt%07d" % (1000000 + n), cands)
live = json.loads(FILE.read_text(encoding="utf-8"))["searches"]
check("at most %d searches are kept" % ps.MAX_SEARCHES, len(live) == ps.MAX_SEARCHES and len(ps._mem) == ps.MAX_SEARCHES)
check("the oldest searches went, the newest stayed", ps.recall("tt1000000", 1) is None and ps.recall("tt1000003", 1) is None
      and ps.recall("tt%07d" % (1000000 + ps.MAX_SEARCHES + 3), 1) is not None)
reset()
many = ranked([stream(n) for n in range(1, 120)])
ps.remember(VID, many)
check("at most %d candidates of one search are kept (a stream_search shows 25 at most)" % ps.MAX_CANDIDATES,
      ps.recall(VID, ps.MAX_CANDIDATES) is not None and ps.recall(VID, ps.MAX_CANDIDATES + 1) is None and ps.MAX_CANDIDATES >= 25)
check("the whole file stays small", FILE.stat().st_size < 400_000)

# =========================================================================== one process or two
reset()
ps.remember(VID, cands)
ps._mem.clear()                                   # what a NEW `jarvis ask` process starts with
check("a search is recalled by a process that never made it (the file mirror)", ps.recall(VID, 2)["infohash"] == cands[1]["infohash"])
check("no temporary file is left next to the mirror", not [p for p in FILE.parent.iterdir() if p.name.endswith(".tmp")])

reset()
ps.remember(VID, cands)
CLOCK[0] += 30
other = json.loads(FILE.read_text(encoding="utf-8"))
other["searches"][VID] = {"at": CLOCK[0], "candidates": [dict(other["searches"][VID]["candidates"][0], label="from the other process")]}
FILE.write_text(json.dumps(other))
check("when the file holds a NEWER search (another process searched again) that one wins", ps.recall(VID, 1)["label"] == "from the other process"
      and ps.recall(VID, 2) is None)
CLOCK[0] += 30
ps.remember(VID, cands)
check("and a newer search here wins over the file", ps.recall(VID, 1)["label"] == cands[0]["label"] and ps.recall(VID, 2) is not None)

reset()
ps.remember("tt0000011", cands)
ps._mem.clear()
ps.remember("tt0000012", cands)
check("remembering in a new process keeps the other process' searches in the file", ps.recall("tt0000011", 1) is not None and ps.recall("tt0000012", 1) is not None)

for junk in (b"\x00\xff garbage", b"", b"[1]", b'{"searches": 3}', b'{"searches": {"tt0000001": {"at": "x", "candidates": 1}}}', b"{" * 40000):
    reset()
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_bytes(junk)
    check("a damaged mirror file (%r...) reads as nothing, no exception" % junk[:10], ps.recall(VID, 1) is None)
    check("...and remembering repairs it", ps.remember(VID, cands) and (ps._mem.clear() or True) and ps.recall(VID, 1) is not None)

reset()
FILE.parent.mkdir(parents=True, exist_ok=True)
FILE.mkdir()
check("a folder where the mirror should be: remember still works in this process, nothing raises", ps.remember(VID, cands) is True
      and ps.recall(VID, 1) is not None)
check("...and no temporary file is left", not [p for p in FILE.parent.iterdir() if p.name.endswith(".tmp")])
FILE.rmdir()

# =========================================================================== threads
reset()
errors = []


def worker(n):
    try:
        vid = "tt%07d" % (2000000 + n)
        for _ in range(20):
            ps.remember(vid, cands)
            ps.recall(vid, 1)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("eight threads remembering and recalling at once raise nothing and keep the bound", not errors and len(ps._mem) <= ps.MAX_SEARCHES)
check("and the file is still readable afterwards", isinstance(json.loads(FILE.read_text(encoding="utf-8"))["searches"], dict))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
