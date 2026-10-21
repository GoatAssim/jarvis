"""Download Hub phase 6: reading and ranking addon stream text (pure, no network).

The wording used here is INVENTED for the test.  It proves the ranking logic and the one marker
table work; it is not evidence of what real addons write (tests/addon_checks.py records that).

Run: python tests/test_hub_stremio_ranking.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-ranking-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub.stremio import markers, ranking  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


def stream(text, h="aa", size=None, **kw):
    out = {"name": "X", "title": text, "infoHash": (h * 20)[:40]}
    if size:
        out["behaviorHints"] = {"videoSize": int(size * 1024 ** 3)}
    out.update(kw)
    return out


def rel(text, h="aa", **kw):
    return ranking.parse_release(stream(text, h, **kw))


# ---- reading the text
r = rel("Show.S02E01.1080p.WEB-DL.DUAL.AUDIO.x265")
check("1080p read", r["resolution"] == 1080)
check("dual audio counts as dub", r["audio"] == "dub")
check("codec and source read", r["codec"] == "x265" and r["source"] == "web-dl")
check("720p read", rel("Show 720p HDTV")["resolution"] == 720)
check("4k read as 2160", rel("Show 4K HDR")["resolution"] == 2160)
check("'Dubbed' is a dub", rel("Show 1080p Dubbed")["audio"] == "dub")
check("'Multi Sub' is a sub, not a dub", rel("Show 1080p Multi Sub")["audio"] == "sub")
check("dual audio AND multi subs is a dub (the audio track is what was asked)", rel("Show 1080p Dual Audio Multi Subs")["audio"] == "dub")
check("no marker = unknown, never dub", rel("Show 1080p WEB-DL")["audio"] == "unknown")
check("'Dubai' does not contain a dub", rel("Dubai.Documentary.1080p")["audio"] == "unknown")
check("CAM is banned", rel("Show 1080p CAM")["banned"].upper() == "CAM")
check("underscores and brackets are spaces", rel("Show_[1080p]_[DUB]")["audio"] == "dub")
check("size from the stream's field", rel("x", size=1.5)["size"] == int(1.5 * 1024 ** 3))
check("size from the text (a decimal point survives)", abs(rel("Show 1080p 1.5 GB")["size"] / 1024 ** 3 - 1.5) < 0.01)
check("seeders from text", rel("Show 1080p \U0001F464 87")["seeders"] == 87)
check("seeders from a field", rel("Show", seeders=12)["seeders"] == 12)
check("a plain url stream is direct, not a torrent", ranking.parse_release({"name": "d", "url": "https://x.invalid/a.mkv"})["direct"])
check("garbage in, None out", ranking.parse_release("nope") is None)
check("the weirdest text doesn't crash", ranking.parse_release({"name": 5, "title": None, "description": ["x"], "infoHash": "zz"}) is not None)

# ---- the request
want, err = ranking.normalize_want("1080p", "dubbed")
check("'1080p' and 'dubbed' normalise", want == {"resolution": 1080, "audio": "dub", "max_gb": None, "min_seeders": None} and err is None)
check("4k -> 2160", ranking.normalize_want("4k", None)[0]["resolution"] == 2160)
check("a bad quality is an error", ranking.normalize_want("999", None)[1] is not None)
check("a bad audio is an error", ranking.normalize_want(None, "klingon")[1] is not None)
check("a bad size is an error", ranking.normalize_want(None, None, "abc")[1] is not None)

# ---- ranking: dub 1080 first, banned out, same torrent once
W = ranking.normalize_want(1080, "dub")[0]
pool = [rel("Show 720p DUB", "01"), rel("Show 1080p Eng Sub", "02", seeders=900), rel("Show 1080p Dubbed", "03", seeders=5),
        rel("Show 1080p CAM Dubbed", "04"), rel("Show 1080p Dubbed", "03"), rel("Show 1080p", "05")]
kept, rejected = ranking.rank(pool, W)
check("the CAM copy is rejected, with a reason", rejected.get("banned token 'CAM'") == 1)
check("the same torrent offered twice is one candidate", len(kept) == 4)
check("dub 1080p wins even against a sub with 900 seeders", kept[0]["tier"] == "exact" and kept[0]["infohash"].startswith("03"))
check("then the same audio at another quality", kept[1]["tier"] == "other_quality" and kept[1]["resolution"] == 720)
check("then the right quality with the other audio / unknown", {c["tier"] for c in kept[2:]} == {"other_audio"})
check("an unknown language is never 'exact' for a dub", all(c["tier"] != "exact" for c in kept if c["audio"] == "unknown"))

# ---- the policies: what is allowed to be queued
no_exact = [c for c in kept if c["tier"] != "exact"]
ask = ranking.choose(no_exact, "ask")
check("policy ask: no exact release -> pending, nothing picked", ask["status"] == "pending" and ask["pick"] is None)
check("...with the options that exist, one per kind", {o["policy"] for o in ask["options"]} == {"other_quality", "other_audio"})
check("policy skip: nothing is queued", ranking.choose(no_exact, "skip")["status"] == "none")
oq = ranking.choose(no_exact, "other_quality")
check("policy other_quality: the dub at 720p, marked fallback", oq["status"] == "fallback" and oq["pick"]["label"] == "720p dub")
oa = ranking.choose(no_exact, "other_audio")
check("policy other_audio: the 1080p sub (or unknown) wins over nothing", oa["status"] == "fallback" and oa["pick"]["resolution"] == 1080)
check("policy best takes the best of what is left", ranking.choose(no_exact, "best")["status"] == "fallback")
exact = ranking.choose(kept, "ask")
check("an exact release is status exact", exact["status"] == "exact" and exact["pick"]["label"] == "1080p dub")
check("fallbacks never include what the policy forbids (ask: only exact releases)", all(f["tier"] == "exact" for f in exact["fallbacks"]))
two_exact = ranking.choose(ranking.rank([rel("Show 1080p Dubbed", "11", seeders=10), rel("Show 1080p Dubbed", "12", seeders=500),
                                         rel("Show 1080p Dubbed", "13", seeders=50)], W)[0], "ask")
check("two fallbacks are kept, best seeded first", two_exact["pick"]["infohash"].startswith("12") and len(two_exact["fallbacks"]) == 2
      and two_exact["fallbacks"][0]["infohash"].startswith("13"))
check("nothing at all -> none", ranking.choose([], "ask")["status"] == "none")

# ---- other requests
sub = ranking.normalize_want(720, "sub")[0]
k2, _ = ranking.rank([rel("Show 720p Eng Sub", "21"), rel("Show 720p Dub", "22")], sub)
check("asking for a sub prefers the sub", k2[0]["audio"] == "sub")
anyw = ranking.normalize_want(1080, None)[0]
k3, _ = ranking.rank([rel("Show 1080p", "31"), rel("Show 720p", "32")], anyw)
check("any audio: the resolution decides", k3[0]["resolution"] == 1080 and k3[0]["tier"] == "exact")
cap = ranking.normalize_want(1080, None, max_gb=1)[0]
k4, rej4 = ranking.rank([rel("Show 1080p", "41", size=3), rel("Show 1080p", "42", size=0.5)], cap)
check("a release over the size cap is rejected", len(k4) == 1 and "over 1.0 GB" in rej4)
seed = ranking.normalize_want(1080, None, min_seeders=20)[0]
k5, _ = ranking.rank([rel("Show 1080p", "51", seeders=3), rel("Show 1080p", "52", seeders=30), rel("Show 1080p", "53")], seed)
check("fewer seeders than the minimum is rejected; unknown seeders are kept", len(k5) == 2)
k6, rej6 = ranking.rank([ranking.parse_release({"name": "d", "title": "Show 1080p DUB", "url": "https://x.invalid/a.mkv"})], W)
check("a direct link is not queued by default", not k6 and sum(rej6.values()) == 1)
k7, _ = ranking.rank([ranking.parse_release({"name": "d", "title": "Show 1080p DUB", "url": "https://x.invalid/a.mkv"})], W, torrents_only=False)
check("...but stream_search can still list it", len(k7) == 1)

# the season's usual encode wins a tie
bg = [rel("Show 1080p Dubbed", "61", seeders=10, behaviorHints={"bingeGroup": "grp-a"}),
      rel("Show 1080p Dubbed", "62", seeders=11, behaviorHints={"bingeGroup": "grp-b"})]
k8, _ = ranking.rank(bg, W, batch_binge="grp-a")
check("the batch's bingeGroup breaks a near tie (+10)", k8[0]["infohash"].startswith("61"))
check("usual_binge needs two episodes to agree", ranking.usual_binge([{"binge": "a"}, {"binge": "a"}, None]) == "a"
      and ranking.usual_binge([{"binge": "a"}]) is None)

# ---- the one table is easy to extend (owner's file, literal words only)
folder = Path(HOME) / ".jarvis" / "hub"
folder.mkdir(parents=True, exist_ok=True)
check("an unknown wording is unknown before the owner adds it", rel("Show 1080p Latino")["audio"] == "unknown")
(folder / "stream_markers.json").write_text(json.dumps({"dub": ["Latino", "english (dub)"], "banned": ["hdrip"],
                                                        "resolution": {"1080": ["fullhd+"]}}))
check("the owner's dub word is read", rel("Show 1080p Latino")["audio"] == "dub")
check("a word with punctuation works literally", rel("Show 1080p english (dub)")["audio"] == "dub")
check("the owner's banned word is read", rel("Show HDRip")["banned"] is not None)
check("the owner's resolution word is read", rel("Show FullHD+")["resolution"] == 1080)
(folder / "stream_markers.json").write_text('{"dub": ["' + "x" * 500 + '", ".*", "(a+)+$"], "sub": "notalist"}')
check("a hostile or broken file can't break ranking (regex characters are literal, long words dropped)",
      rel("Show 1080p aaaaaaaaaaaaaaaaaaaaaaaa!")["audio"] == "unknown")
(folder / "stream_markers.json").write_text("{not json")
check("an unreadable file is ignored", rel("Show 1080p Dubbed")["audio"] == "dub")
check("every marker in the table is honestly flagged unverified in the module", "UNVERIFIED" in markers.__doc__)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
