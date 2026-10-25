"""Round 8, brief 8B (N28, N30): the play intent, packs flagged, a `language` the owner can ask for.
Pure: no addon, no network.

The ten stream names come from ONE real turn (finding 04) and are stored cut at 80 characters in
tests/data/stream-names-175.json.  The OLD orders pinned below were computed once by running the
untouched jarvis-main_175_ code on those names; they are what `intent="bulk"` (the default) must keep
giving.  Everything else here is built from invented text, to prove the logic only: it is not
evidence of what other addons write.

Run: python tests/test_hub_stremio_ranking_play.py
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-ranking-play-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub.stremio import markers, ranking  # noqa: E402

passed = failed = 0
GB = 1024 ** 3


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


# --------------------------------------------------------------------------- the logged names
DATA = json.loads((ROOT / "tests" / "data" / "stream-names-175.json").read_text(encoding="utf-8"))
ROWS = DATA["rows"]


def logged(rank_logged):
    return next(r for r in ROWS if r["rank_logged"] == rank_logged)


def logged_releases(episode=None):
    """The ten rows as parsed releases.  The '(trimmed)' mark is the log's, not the addon's: dropped.
    The cut name is all the text there is: no fileIdx and no filename is known for any row."""
    out = []
    for r in ROWS:
        stream = {"name": r["name"].replace("…(trimmed)", ""), "infoHash": ("%02x" % r["rank_logged"]) * 20,
                  "behaviorHints": {"videoSize": int(r["size_gb"] * GB)}}
        if r["seeders"] is not None:
            stream["seeders"] = r["seeders"]
        rel = ranking.parse_release(stream, episode=episode)
        rel["addon"] = r["addon"]
        rel["rk"] = r["rank_logged"]
        out.append(rel)
    return out


def order_of(kept):
    return [c["rk"] for c in kept]


check("the data file has the ten rows and says they are cut at 80 characters from one real turn",
      len(ROWS) == 10 and "80 characters" in DATA["header"]["cut"] and "ONE real" in DATA["header"]["what"])
check("the logged names are the ones finding 04 shows (rank 4 is the S01E03 one)", "S01E03" in logged(4)["name"])

# --------------------------------------------------------------------------- bulk intent = the old order
OLD = {
    ("1080p", "dub"): [1, 2, 3, 4, 5, 7, 9, 6, 8, 10],
    ("1080p", None): [1, 2, 3, 4, 5, 7, 6, 8, 9, 10],
    (None, None): [1, 2, 7, 3, 9, 4, 6, 5, 8, 10],
    ("720p", "dub"): [7, 6, 8, 1, 2, 3, 9, 4, 5, 10],
}
OLD_TIERS_1080_DUB = ["other_audio"] * 5 + ["other_both"] * 5
OLD_SCORES_1080_DUB = [50.894, 49.633, 48.0, 47.634, 40.0, 9.225, 8.0, 7.634, 0.0, 0.0]
OLD_LABELS = {1: "1080p, language not stated", 2: "1080p, language not stated", 3: "1080p, language not stated",
              4: "1080p, language not stated", 5: "1080p, language not stated", 6: "720p, language not stated",
              7: "720p, language not stated", 8: "720p, language not stated",
              9: "quality not stated, language not stated", 10: "quality not stated, language not stated"}

for (quality, audio), expected in OLD.items():
    want, err = ranking.normalize_want(quality, audio)
    kept, rejected = ranking.rank(logged_releases(), want)
    check("bulk (the default) keeps the old order for %s / %s" % (quality, audio), order_of(kept) == expected and not rejected)
    kept2, _ = ranking.rank(logged_releases(), want, intent="bulk")
    check("...and intent='bulk' written out gives the same" , order_of(kept2) == expected)
    kept3, _ = ranking.rank(logged_releases(), want, intent="something else")
    check("...and an unknown intent is the bulk order too (the safe side)", order_of(kept3) == expected)

W_DUB = ranking.normalize_want("1080p", "dub")[0]
kept, _ = ranking.rank(logged_releases(), W_DUB)
check("the old tiers are unchanged on the logged names", [c["tier"] for c in kept] == OLD_TIERS_1080_DUB)
check("the old scores are unchanged on the logged names", [round(c["score"], 3) for c in kept] == OLD_SCORES_1080_DUB)
check("the old labels are unchanged (language unset: no suffix)", all(c["label"] == OLD_LABELS[c["rk"]] for c in kept))
check("language unset: language_match is None on every candidate", all(c["language_match"] is None for c in kept))
check("want without a language is exactly the old dict (no 'language' key)",
      ranking.normalize_want("1080p", "dubbed")[0] == {"resolution": 1080, "audio": "dub", "max_gb": None, "min_seeders": None})
check("a bulk-style call with the 4 old positional arguments still works",
      ranking.normalize_want("1080p", "dub", 2, 5)[0] == {"resolution": 1080, "audio": "dub", "max_gb": 2.0, "min_seeders": 5})

# --------------------------------------------------------------------------- the facts on the logged names
rels = {r["rk"]: r for r in logged_releases()}
check("packs flagged: rows 1, 2, 3 (Saison 1, Temporada Completa, [S01-S08]) are packs",
      all(rels[n]["pack"] for n in (1, 2, 3)))
check("packs flagged: rows 6, 7 (Temporada) and 9 (a bare S01) are packs too", all(rels[n]["pack"] for n in (6, 7, 9)))
check("row 4 (a season word AND the S01E03 token) is not a pack", rels[4]["pack"] is False)
check("rows 5, 8, 10 ('[P2P] Torz 1080p' and the like) say nothing: not a pack", not any(rels[n]["pack"] for n in (5, 8, 10)))
check("a pack is never decided from the size (row 4 is 0.35 GB, row 1 is 1.55 GB)", rels[4]["size"] < rels[1]["size"] and rels[1]["pack"] and not rels[4]["pack"])
check("file_known: only row 4 (its text names one S01E03); nothing else is known",
      [n for n in sorted(rels) if rels[n]["file_known"]] == [4] and rels[4]["file_known_by"] == "text")
check("languages read from the logged words only",
      rels[1]["languages"] == ["french"] and rels[2]["languages"] == ["portuguese"] and rels[3]["languages"] == []
      and rels[4]["languages"] == ["french"] and rels[5]["languages"] == [] and rels[6]["languages"] == ["portuguese"]
      and rels[7]["languages"] == ["portuguese"] and rels[8]["languages"] == []
      and rels[9]["languages"] == ["english", "czech", "hungarian"] and rels[10]["languages"] == [])
check("every candidate fact is JSON-safe (the rows the model sees)",
      all(json.dumps({k: v for k, v in r.items() if k != "stream"}) for r in rels.values()))

# --------------------------------------------------------------------------- play intent (N28)
W_ANY = ranking.normalize_want("1080p", None)[0]
kept, _ = ranking.rank(logged_releases(), W_ANY, intent="play")
check("play intent: the S01E03 release is first (1080p, any audio)", kept[0]["rk"] == 4)
check("play intent: the rest of the tier keeps the old order, other tiers untouched",
      order_of(kept) == [4, 1, 2, 3, 5, 7, 6, 8, 9, 10])
check("play intent: the packs are flagged in the candidates", [c["pack"] for c in kept if c["rk"] in (1, 2, 3)] == [True] * 3)
check("play intent does not move a release across a tier", [c["tier"] for c in kept][:5] == ["exact"] * 5 and kept[5]["tier"] == "other_quality")
kept, _ = ranking.rank(logged_releases(), W_DUB, intent="play")
check("play intent with 1080p dub (nothing exact on the cut names): row 4 still leads its tier",
      order_of(kept) == [4, 1, 2, 3, 5, 7, 9, 6, 8, 10])
kept, _ = ranking.rank(logged_releases(), ranking.normalize_want(None, None)[0], intent="play")
check("play intent with no quality: row 4 first, the old order after it", order_of(kept) == [4, 1, 2, 7, 3, 9, 6, 5, 8, 10])
kept, _ = ranking.rank(logged_releases(), W_ANY, intent="play", episode=(1, 5))
check("play intent for ANOTHER episode (S01E05): row 4's token is a different episode, so it is a pack and not known",
      order_of(kept) == OLD[("1080p", None)] and kept[3]["pack"] is True and kept[3]["file_known"] is False)
kept, _ = ranking.rank(logged_releases(), W_ANY, intent="play", episode=(1, 3))
check("play intent for S01E03 with the episode given: the same first row, pack False",
      kept[0]["rk"] == 4 and kept[0]["pack"] is False and kept[0]["file_known_by"] == "text")
check("a bad episode argument is ignored, never an error",
      order_of(ranking.rank(logged_releases(), W_ANY, intent="play", episode="x")[0]) == [4, 1, 2, 3, 5, 7, 6, 8, 9, 10])


def stream(text, h="aa", size=None, **kw):
    out = {"name": "X", "title": text, "infoHash": (h * 20)[:40]}
    if size:
        out["behaviorHints"] = {"videoSize": int(size * GB)}
    out.update(kw)
    return out


def rel(text, h="aa", **kw):
    return ranking.parse_release(stream(text, h, **kw))


# --- N28a: file known always first inside a tier, however many seeders the other one has
fast_pack = rel("Show Season 1 1080p", "b1", seeders=500)
named_pack = rel("Show Season 1 1080p", "b2", seeders=3, fileIdx=2)
single = rel("Show S01E03 1080p", "b3", seeders=20)
kept, _ = ranking.rank([fast_pack, named_pack, single], W_ANY, intent="play")
order_ids = [c["infohash"][:2] for c in kept]
check("N28a: a 3-seeder pack that NAMES its file comes before a 500-seeder pack whose file must be found",
      order_ids.index("b2") < order_ids.index("b1") and kept[order_ids.index("b2")]["pack"] and kept[order_ids.index("b2")]["file_known_by"] == "file_idx")
check("...file-known releases first (the 20-seeder single episode, then the named pack), the unknown pack last",
      order_ids == ["b3", "b2", "b1"])
kept, _ = ranking.rank([fast_pack, named_pack, single], W_ANY)
check("the same three in bulk: seeders decide, as before", [c["infohash"][:2] for c in kept] == ["b1", "b3", "b2"])
check("a pack with a fileIdx is a pack AND file_known", named_pack["pack"] and named_pack["file_known"])
check("fileIdx 0 counts (it is a real index)", rel("Show 1080p", "b4", fileIdx=0)["file_known"])
check("fileIdx -1 does not count (it means 'pick')", not rel("Show 1080p", "b5", fileIdx=-1)["file_known"])
check("a filename with the SxxExx counts, and says so",
      rel("Show 1080p", "b6", behaviorHints={"filename": "Show.S01E03.1080p.mkv"})["file_known_by"] == "filename")
check("a filename for another episode does not count when the episode is given",
      ranking.rank([rel("Show 1080p", "b7", behaviorHints={"filename": "Show.S01E02.1080p.mkv"})], W_ANY, intent="play", episode=(1, 3))[0][0]["file_known"] is False)
check("a filename with a range names many files: not known",
      rel("Show 1080p", "b8", behaviorHints={"filename": "Show.S01E01-E10.mkv"})["file_known"] is False)
check("play intent never rejects or dedupes differently: same count in and out",
      len(ranking.rank(logged_releases(), W_ANY, intent="play")[0]) == len(ranking.rank(logged_releases(), W_ANY)[0]) == 10)
check("rank does not change the releases it is given", all("tier" not in r for r in logged_releases()))

# --------------------------------------------------------------------------- the pack words and their edges
PACK_CASES = [
    ("Show Season 10 1080p", True), ("Show S10 1080p", True), ("Show S01E03 1080p", False), ("Show S1E3 1080p", False),
    ("Show S01-S08 1080p", True), ("Show [S01-S08] [MULTi]", True), ("Show Complete Series 1080p", True),
    ("Show 1080p", False), ("Show S01E01-E10 1080p", True), ("Show S01E01E02 1080p", True), ("Show S01E03 S01E04", True),
    ("Show Saison 1 S01E03", False), ("Show Season 1 S01E03", False), ("Show Incomplete 1080p", False),
    ("Show Seasons 1080p", False), ("Show 1ª Temporada Completa", True), ("Show S01 1080p", True),
]
for text, expected in PACK_CASES:
    check("pack %-5s <- %r" % (expected, text), rel(text)["pack"] is expected)
check("a 40 GB name with no pack word is still not a pack (size decides nothing)", rel("Show 1080p", size=40)["pack"] is False)
check("S01E03 is read as ONE episode, S01 alone as none", rel("Show S01E03")["facts"]["episodes"] == [[1, 3, 3]] and rel("Show S01")["facts"]["episodes"] == [])
check("S01E01-E10 is read as a range", rel("Show S01E01-E10")["facts"]["episodes"] == [[1, 1, 10]])

# --------------------------------------------------------------------------- language (N30)
W_EN = ranking.normalize_want("1080p", None, language="english")[0]
check("language='english' is kept in want", W_EN.get("language") == "english")
for typed in ("English", " ENG ", "en"):
    check("%r is english" % typed, ranking.normalize_want(None, None, language=typed)[0].get("language") == "english")
for typed in (None, "", "any", "ANY"):
    check("language %r means no language: the key is absent" % (typed,), "language" not in ranking.normalize_want(None, None, language=typed)[0])
want, problem = ranking.normalize_want("1080p", "dub", language="klingon")
check("an unknown language is a plain sentence, like the other problems",
      want is None and "language must be one of english" in problem and "klingon" in problem and "\n" not in problem)
check("a language that is not even text is a sentence too", ranking.normalize_want(None, None, language=["a"])[1] is not None
      and ranking.normalize_want(None, None, language=5)[1] is not None)
want, problem = ranking.normalize_want("1080p", "sub", language="english")
check("a language with audio=sub is a plain sentence (subtitle languages are not read), never a silent drop",
      want is None and "audio=sub" in problem and "\n" not in problem)
check("a language with audio dub or any is fine",
      ranking.normalize_want(None, "dub", language="english")[1] is None and ranking.normalize_want(None, "any", language="english")[1] is None)
check("'dub' is not a language (audio='english' still means audio dub, as before; no language is set)",
      ranking.normalize_want(None, "english")[0] == {"resolution": None, "audio": "dub", "max_gb": None, "min_seeders": None})

kept, rejected = ranking.rank(logged_releases(), W_EN)
check("language=english on the logged names: nothing is 'exact' (no cut name names English at 1080p)",
      not any(c["tier"] == "exact" for c in kept) and not rejected)
check("...so no French- or Portuguese-worded release is in the exact tier",
      all(c["tier"] != "exact" for c in kept if c["rk"] in (1, 2, 4, 6, 7)))
check("named (row 9) above every multi (rows 1, 3, 4, 7); multi above no inside a tier",
      order_of(kept) == [9, 1, 3, 4, 2, 5, 7, 6, 8, 10])
by = {c["rk"]: c for c in kept}
check("language_match on the logged names",
      by[9]["language_match"] == "named" and all(by[n]["language_match"] == "multi" for n in (1, 3, 4, 7))
      and all(by[n]["language_match"] == "no" for n in (2, 5, 6, 8, 10)))
check("the label says why (named / multi / not named)",
      by[9]["label"].endswith("(English named)") and by[1]["label"].endswith("(multi-language, English not confirmed)")
      and by[2]["label"].endswith("(English not named)"))
check("a MULTI release is not counted as English: policy 'ask' gives 'pending', nothing picked",
      ranking.choose(kept, "ask")["status"] == "pending" and ranking.choose(kept, "ask")["pick"] is None)
check("...and the options still show what exists", len(ranking.choose(kept, "ask")["options"]) >= 2)
kept, _ = ranking.rank(logged_releases(), W_EN, intent="play")
check("language and play together: named first, then multi with the file-known one first",
      order_of(kept) == [9, 4, 1, 3, 2, 5, 7, 6, 8, 10])

# the same on invented text, where 'exact' can happen
W_EN_DUB = ranking.normalize_want("1080p", "dub", language="english")[0]
pool = [rel("Show 1080p Dubbed Saison 1", "c1", seeders=900), rel("Show 1080p DUB MULTI", "c2", seeders=50),
        rel("Show 1080p English Dub", "c3", seeders=5), rel("Show 1080p Dubbed", "c4", seeders=100)]
kept, _ = ranking.rank(pool, W_EN_DUB)
check("dub + english: only the release that names English is exact, however few seeders it has",
      kept[0]["infohash"].startswith("c3") and kept[0]["tier"] == "exact" and [c["tier"] for c in kept[1:]] == ["other_audio"] * 3)
check("...then the multi one, then the French-worded one, then the one that says nothing",
      [c["language_match"] for c in kept] == ["named", "multi", "no", "no"] and kept[1]["infohash"].startswith("c2"))
check("a 'dub' alone is still a dub, never English", rel("Show 1080p Dubbed")["audio"] == "dub" and rel("Show 1080p Dubbed")["languages"] == [])
kept, _ = ranking.rank(pool, ranking.normalize_want("1080p", "dub")[0])
check("without a language the same pool is as before: all four are exact, seeders decide",
      [c["tier"] for c in kept] == ["exact"] * 4 and kept[0]["infohash"].startswith("c1"))

for text, expected in (("Show 1080p ENG", ["english"]), ("Show [ENG] 1080p", ["english"]), ("Show 1080p English", ["english"]),
                       ("Show 1080p \U0001F1EC\U0001F1E7", ["english"]), ("Show 1080p \U0001F1FA\U0001F1F8", ["english"]),
                       ("Show 1080p Eng Dub", ["english"]), ("Show 1080p Eng Sub", []), ("Show 1080p ENGSUB", []),
                       ("Show 1080p English Sub", []), ("Show 1080p Multi Sub", []), ("Show 1080p Engineering", []),
                       ("Show 1080p Saison 2", ["french"]), ("Show 1080p eng cze hun", ["english", "czech", "hungarian"])):
    check("languages of %r -> %s" % (text, expected), rel(text)["languages"] == expected)
check("'Multi Sub' is subtitles: not a multi-language marker", ranking.rank([rel("Show 1080p Multi Sub")], W_EN)[0][0]["language_match"] == "no")
check("a MULTI / DUAL marker is 'multi', never 'named'",
      ranking.rank([rel("Show 1080p MULTI")], W_EN)[0][0]["language_match"] == "multi"
      and ranking.rank([rel("Show 1080p [DUAL]")], W_EN)[0][0]["language_match"] == "multi")
check("unknown text never counts as English: nothing stated is 'no' with an empty languages list",
      ranking.rank([rel("Show 1080p")], W_EN)[0][0]["language_match"] == "no" and rel("Show 1080p")["languages"] == [])
W_FR = ranking.normalize_want("1080p", None, language="french")[0]
check("the other languages work the same way (french: Saison names it)",
      ranking.rank([rel("Show Saison 1 1080p")], W_FR)[0][0]["language_match"] == "named")

# --------------------------------------------------------------------------- markers: additive, and only logged words
check("the old tables are untouched (DUB)", markers.DUB == (
    r"dub", r"dubbed", r"dubs", r"dual[\s-]?audio", r"dualaudio", r"multi[\s-]?audio", r"multiaudio",
    r"english[\s-]?audio", r"eng[\s-]?audio", r"eng[\s-]?dub", r"english[\s-]?dub"))
check("the old tables are untouched (SUB)", markers.SUB == (
    r"sub", r"subs", r"subbed", r"softsub", r"hardsub", r"eng[\s-]?sub", r"engsub", r"english[\s-]?sub",
    r"multi[\s-]?sub", r"multisub", r"jpn[\s-]?audio", r"japanese[\s-]?audio", r"jap[\s-]?audio"))
check("the old tables are untouched (BANNED, RESOLUTION)", len(markers.BANNED) == 12 and markers.BANNED[0] == "cam"
      and [h for h, _p in markers.RESOLUTION] == [2160, 1080, 720, 480])
joined = " ".join(r["name"] for r in ROWS).lower()
words = list(markers.PACK) + list(markers.MULTI) + [w for _n, ws in markers.LANGUAGE for w in ws]
check("every new word in markers.py is in at least one logged name: " + ", ".join(words),
      all(re.search(r"(?<![0-9a-z])" + w + r"(?![0-9a-z])", joined.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ").replace(".", " ")) for w in words))
norm = [markers.normalise(r["name"]) for r in ROWS]
check("the season / range pattern matches a logged name (S01-S08, S01)",
      any(re.search(r"(?<![0-9a-z])" + markers.SEASON + r"(?![0-9a-z])", n) for n in norm[2:3] + norm[8:9]))
check("the episode pattern matches the logged S01E03", re.search(r"(?<![0-9a-z])" + markers.EPISODE + r"(?![0-9a-z])", norm[3]) is not None)
folder = Path(HOME) / ".jarvis" / "hub"
folder.mkdir(parents=True, exist_ok=True)
(folder / "stream_markers.json").write_text('{"dub": ["Latino"]}')
check("the owner's stream_markers.json still extends dub as before, and adds no language", rel("Show 1080p Latino")["audio"] == "dub"
      and rel("Show 1080p Latino")["languages"] == [])
(folder / "stream_markers.json").unlink()

# --------------------------------------------------------------------------- hostile text
HOSTILE = ["\x00\x01\x1b[31mS01E03\x07\x08", "S01E" * 1250, "s1-" * 1700, "eng " * 1300, "(" * 5000, "\U0001F1EC" * 2500,
           "A" * 5000, "Season " * 800 + "S01E03", "S" + "9" * 5000 + "E" + "9" * 5000, "\u202e" + "multi" * 1000]
began = time.time()
raised = None
try:
    for text in HOSTILE:
        for stream_obj in ({"name": text}, {"name": "x", "title": text, "behaviorHints": {"filename": text}, "infoHash": "ab" * 20, "fileIdx": text}):
            r = ranking.parse_release(stream_obj)
            ranking.rank([r], W_EN, intent="play", episode=(1, 3))
            ranking.rank([r], W_ANY, intent="play")
            ranking.rank([r], W_ANY)
except Exception as exc:  # noqa: BLE001
    raised = exc
check("hostile text (control characters, 5,000-character names, huge numbers) never raises", raised is None)
check("...and is quick (under 5 seconds for all of it)", time.time() - began < 5)
check("a 5,000-character name gives at most %d episode tokens" % ranking._EPISODE_CAP,
      len(ranking.parse_release({"name": "S01E01 " * 700})["facts"]["episodes"]) <= ranking._EPISODE_CAP)
check("odd fields never raise",
      ranking.parse_release({"name": 5, "title": None, "description": ["x"], "infoHash": "zz", "behaviorHints": {"filename": 7}}) is not None
      and ranking.parse_release("nope") is None)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
