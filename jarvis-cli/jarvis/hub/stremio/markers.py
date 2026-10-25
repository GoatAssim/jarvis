"""The one small table that turns an addon's stream text into facts (plan 9.3, brief 07, N13).

An addon stream has no "audio language" or "resolution" field.  Both are written in the stream's
`name`, `description` / `title` and `behaviorHints.filename`, and every addon words them its own
way.  This file is where those words live, so a new addon is a one-line fix and nothing else in
the hub knows a single marker.

    !! HONEST STATUS (brief 07, round 1) !!
    Every pattern below is UNVERIFIED.  The agent that wrote this had no network and no real
    addon answer to copy from, and the brief says markers must come from real answers, never
    from memory.  So each row carries `seen = False`: it is a plain-language guess at how
    release names are usually written ("DUAL AUDIO", "1080p", "WEB-DL").  `tests/addon_checks.py`
    (owner runs it on their PC) records the real answers of the addons they have installed; fix
    this table from that file, flip `seen` to True per row, and the ranking follows.

The owner can extend the table without touching code: `~/.jarvis/hub/stream_markers.json`

    {"dub": ["english dub"], "sub": ["engsub"], "banned": ["hdrip"], "resolution": {"1080": ["fhd"]}}

Entries there are LITERAL words (matched whole, case-insensitive), never regular expressions, and
at most 40 per list of at most 40 characters each, so a broken or hostile file can't break ranking.
"""

import json
import re

from .. import paths

# --------------------------------------------------------------------------- the table

# (resolution in pixels of height, regex).  Longest first.  `seen=False`: see the header.
RESOLUTION = (
    (2160, r"(?<![0-9a-z])(?:2160[\s-]?[pi]|4k|uhd)(?![0-9a-z])"),
    (1080, r"(?<![0-9a-z])(?:1080[\s-]?[pi]|fhd|full[\s-]?hd)(?![0-9a-z])"),
    (720, r"(?<![0-9a-z])(?:720[\s-]?[pi])(?![0-9a-z])"),
    (480, r"(?<![0-9a-z])(?:480[\s-]?[pi]|576[\s-]?[pi])(?![0-9a-z])"),
)

# Audio.  A DUB marker means there is an English (or other dubbed) AUDIO track in the file.
# "multi sub" / "eng sub" mean subtitles only.  Both present ("dual audio, multi subs") = dub:
# the audio track is what the owner asked for.  Neither present = "unknown", never "dub".
DUB = (
    r"dub", r"dubbed", r"dubs", r"dual[\s-]?audio", r"dualaudio", r"multi[\s-]?audio", r"multiaudio",
    r"english[\s-]?audio", r"eng[\s-]?audio", r"eng[\s-]?dub", r"english[\s-]?dub",
)
SUB = (
    r"sub", r"subs", r"subbed", r"softsub", r"hardsub", r"eng[\s-]?sub", r"engsub", r"english[\s-]?sub",
    r"multi[\s-]?sub", r"multisub", r"jpn[\s-]?audio", r"japanese[\s-]?audio", r"jap[\s-]?audio",
)

# A release that is not a usable copy.  Hard filter.
BANNED = (r"cam", r"hdcam", r"camrip", r"ts", r"hdts", r"telesync", r"tc", r"telecine", r"3d",
          r"scr", r"screener", r"hdtv[\s-]?rip[\s-]?sd")

# Smaller facts for the plan table and tie-breaks.
CODEC = (("x265", r"x265|h[\s-]?265|hevc"), ("x264", r"x264|h[\s-]?264|avc"), ("av1", r"av1"))
SOURCE = (("remux", r"remux"), ("bluray", r"blu[\s-]?ray|bdrip|brrip|bd"), ("web-dl", r"web[\s-]?dl|webdl"),
          ("webrip", r"web[\s-]?rip|webrip"), ("hdtv", r"hdtv"))

# Seeders and size written in text, for addons that don't put them in a field.  Several shapes;
# the first match wins.
SEEDERS = (r"(?:seeders?|seeds?)\s*[:=]?\s*(\d{1,7})", r"\U0001F464\s*(\d{1,7})")
SIZE = r"(\d{1,4}(?:[.,]\d{1,2})?)[\s-]?(gib|gb|mib|mb)"

# --------------------------------------------------------------------------- packs and languages (N28, N30; brief 8B)
#
# Every word below was READ OFF the ten stream names of ONE real turn: finding 04, copied to
# `tests/data/stream-names-175.json` ("row N" = the `rank_logged` there).  One show, one set of addons
# (Torrentio, StremThru Torz, AIOStreams), names CUT at 80 characters.  A word that no logged name
# shows is NOT in this file (the few the brief names but no logged name shows live in ranking.py,
# marked there).  The owner's `stream_markers.json` does not extend these lists.
#
# a release's own word for "a whole season":  Saison (rows 1, 4), Temporada (rows 2, 6, 7), Completa (rows 2, 6)
PACK = (r"saison", r"temporada", r"completa")
# a season token or a range of seasons: `S01` (row 9), `S01-S08` (row 3).  Not `S01E03`: a letter may not follow.
SEASON = r"s(\d{1,2})(?:[\s._-]?(?:-|to)[\s._-]?s?(\d{1,2}))?"
# an episode token: `S01E03` (row 4).  Ranges (`S01E01-E10`, `S01E01E02`) are read too; no logged name shows one.
EPISODE = r"s(\d{1,2})[\s._-]?e(\d{1,3})(?:[\s._-]?(?:-|e)[\s._-]?e?(\d{1,3}))?"
# "more than one language in the file": MULTI / MULTi / Multi (rows 1, 3, 4, 9), [DUAL] (row 7)
MULTI = (r"multi", r"dual")
# a language the text names: (name, words).  `eng` (row 9; `cze` and `hun` too), `saison` is French (rows 1, 4),
# `temporada` is Portuguese (rows 2, 6, 7: written by Spanish releases too, see the 8B report).
LANGUAGE = (
    ("english", (r"eng",)),
    ("french", (r"saison",)),
    ("portuguese", (r"temporada",)),
    ("czech", (r"cze",)),
    ("hungarian", (r"hun",)),
)

MAX_EXTRA_WORDS = 40
MAX_EXTRA_LEN = 40
EXTRA_FILE = "stream_markers.json"


def _wrap(alternatives):
    return re.compile(r"(?<![0-9a-z])(?:%s)(?![0-9a-z])" % "|".join(alternatives), re.I)


def _literal(word):
    """A literal owner word -> a pattern fragment (spaces and dots may be any run of them)."""
    parts = [re.escape(p) for p in re.split(r"[\s._-]+", word.strip().lower()) if p]
    return r"[\s._-]?".join(parts)


def _extra_file():
    try:
        path = paths.hub_dir() / EXTRA_FILE
        if not path.exists() or path.stat().st_size > 64 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _words(values):
    out = []
    for raw in (values or [])[:MAX_EXTRA_WORDS] if isinstance(values, list) else []:
        text = str(raw or "").strip().lower()
        if text and len(text) <= MAX_EXTRA_LEN and re.search(r"[a-z0-9]", text):
            out.append(_literal(text))
    return out


def compiled():
    """The table as compiled patterns, with the owner's extra words added.  Cheap enough to call
    per search (a few regexes); the file is read each time so an edit needs no restart."""
    extra = _extra_file()
    resolution = []
    extra_res = extra.get("resolution") if isinstance(extra.get("resolution"), dict) else {}
    for height, pattern in RESOLUTION:
        more = _words(extra_res.get(str(height)))
        resolution.append((height, re.compile(pattern + (("|(?<![0-9a-z])(?:%s)(?![0-9a-z])" % "|".join(more)) if more else ""), re.I)))
    return {
        "resolution": resolution,
        "dub": _wrap(list(DUB) + _words(extra.get("dub"))),
        "sub": _wrap(list(SUB) + _words(extra.get("sub"))),
        "banned": _wrap(list(BANNED) + _words(extra.get("banned"))),
        "codec": [(name, re.compile(r"(?<![0-9a-z])(?:%s)(?![0-9a-z])" % pat, re.I)) for name, pat in CODEC],
        "source": [(name, re.compile(r"(?<![0-9a-z])(?:%s)(?![0-9a-z])" % pat, re.I)) for name, pat in SOURCE],
        "seeders": [re.compile(p, re.I) for p in SEEDERS],
        "size": re.compile(SIZE, re.I),
        # N28 / N30 (brief 8B): packs and languages
        "pack": _wrap(list(PACK)),
        "season": re.compile(r"(?<![0-9a-z])" + SEASON + r"(?![0-9a-z])", re.I),
        "episode": re.compile(r"(?<![0-9a-z])" + EPISODE + r"(?![0-9a-z])", re.I),
        "multi": _wrap(list(MULTI)),
        "language": [(name, _wrap(list(words))) for name, words in LANGUAGE],
    }


def normalise(text):
    """Release names use dots, underscores and brackets as spaces.  A dot between two digits
    ("1.5 GB") is a decimal point and stays."""
    text = re.sub(r"[\[\]\(\)_,|/\\]+", " ", str(text or "")).lower()
    return re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
