"""Rank a Stremio addon's streams for one episode (plan 9.3, brief 07, N13).  Pure: no network.

    parse_release(stream)      -> the facts in one stream (resolution, audio, size, seeders ...)
    rank(releases, want, ...)  -> [candidate] best first, with the reason each one is not exact
    choose(...)                -> what one episode gets: exact / fallback / pending / none

The request is `want = {"resolution": 1080, "audio": "dub", "max_gb": None, "min_seeders": None}`.

Order of preference for one episode, always:

    exact          the resolution AND the audio the owner asked for
    other_quality  the audio asked for, another resolution (the closest lower one first, then higher)
    other_audio    the resolution asked for, the other audio (or "language not stated")
    other_both     neither

What is ALLOWED to be queued is the `on_missing` policy's business (see POLICY_TIERS): `ask` and
`skip` allow only `exact`, so a plan never quietly swaps a dub for a sub.  The words the markers
are matched against come from `markers.py`, the one table that is easy to extend.

Streams are untrusted text.  Nothing here evaluates it: it is matched against fixed patterns and
the results are numbers, short labels and booleans.
"""

import math
import re
from collections import Counter

from . import links, markers
from .. import states

AUDIO_VALUES = ("dub", "sub", "any")
RESOLUTIONS = (2160, 1080, 720, 480)
POLICIES = ("ask", "skip", "other_quality", "other_audio", "best")
TIERS = ("exact", "other_quality", "other_audio", "other_both")

# which tiers each `on_missing` policy lets through (the order is also the preference order)
POLICY_TIERS = {
    "ask": ("exact",),
    "skip": ("exact",),
    "other_quality": ("exact", "other_quality"),
    "other_audio": ("exact", "other_audio"),
    "best": TIERS,
}

MAX_FALLBACKS = 2
_GB = 1024 ** 3
_MB = 1024 ** 2


# --------------------------------------------------------------------------- the request

def normalize_want(resolution=None, audio=None, max_gb=None, min_seeders=None):
    """The owner's words -> ({want}, error).  \"1080p\", 1080 and \"full hd\" are one thing."""
    out = {"resolution": None, "audio": "any", "max_gb": None, "min_seeders": None}
    if resolution not in (None, "", "any"):
        text = str(resolution).strip().lower().replace("p", "").replace("i", "")
        text = {"4k": "2160", "uhd": "2160", "fhd": "1080", "fullhd": "1080", "hd": "720", "sd": "480"}.get(text, text)
        if not text.isdigit() or int(text) not in RESOLUTIONS:
            return None, "quality must be one of 2160, 1080, 720, 480 (or any); got %r" % str(resolution)[:20]
        out["resolution"] = int(text)
    if audio not in (None, ""):
        text = str(audio).strip().lower()
        text = {"dubbed": "dub", "dubs": "dub", "english": "dub", "subbed": "sub", "subs": "sub", "subtitles": "sub",
                "original": "sub", "either": "any", "none": "any"}.get(text, text)
        if text not in AUDIO_VALUES:
            return None, "audio must be dub, sub or any; got %r" % str(audio)[:20]
        out["audio"] = text
    if max_gb not in (None, ""):
        try:
            number = float(max_gb)
        except (TypeError, ValueError):
            return None, "max_gb must be a number of gigabytes per episode"
        if not (0 < number <= 500):
            return None, "max_gb must be between 0 and 500"
        out["max_gb"] = number
    if min_seeders not in (None, ""):
        try:
            number = int(min_seeders)
        except (TypeError, ValueError):
            return None, "min_seeders must be a whole number"
        if not (0 <= number <= 100000):
            return None, "min_seeders must be between 0 and 100000"
        out["min_seeders"] = number or None
    return out, None


# --------------------------------------------------------------------------- one stream

def _text_of(stream):
    hints = stream.get("behaviorHints") if isinstance(stream.get("behaviorHints"), dict) else {}
    parts = [stream.get("name"), stream.get("title"), stream.get("description"), hints.get("filename")]
    return " ".join(str(p) for p in parts if isinstance(p, (str, int, float)) and str(p).strip())[:4000]


def _size_bytes(stream, text, table):
    hints = stream.get("behaviorHints") if isinstance(stream.get("behaviorHints"), dict) else {}
    size = hints.get("videoSize")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
        return int(size)
    match = table["size"].search(text)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    unit = match.group(2).lower()
    return int(number * (_GB if unit in ("gb", "gib") else _MB))


def _seeders(stream, text, table):
    for key in ("seeders", "seeds", "peers"):
        value = stream.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    for pattern in table["seeders"]:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def parse_release(stream, table=None):
    """One Stremio stream object -> the facts ranking needs.  Never raises."""
    table = table or markers.compiled()
    if not isinstance(stream, dict):
        return None
    raw = _text_of(stream)
    text = markers.normalise(raw)
    hints = stream.get("behaviorHints") if isinstance(stream.get("behaviorHints"), dict) else {}
    parsed = links.parse_stream(stream)
    resolution = None
    for height, pattern in table["resolution"]:
        if pattern.search(text):
            resolution = height
            break
    dub, sub = bool(table["dub"].search(text)), bool(table["sub"].search(text))
    audio = "dub" if dub else ("sub" if sub else "unknown")
    banned = table["banned"].search(text)
    url = stream.get("url")
    return {
        "resolution": resolution, "audio": audio,
        "codec": next((n for n, p in table["codec"] if p.search(text)), None),
        "source": next((n for n, p in table["source"] if p.search(text)), None),
        "size": _size_bytes(stream, text, table), "seeders": _seeders(stream, text, table),
        "banned": banned.group(0) if banned else None,
        "binge": states.clean_text(hints.get("bingeGroup"), 80) if hints.get("bingeGroup") else None,
        "torrent": parsed is not None, "direct": isinstance(url, str) and bool(url),
        "infohash": parsed["infohash"] if parsed else None, "file_idx": parsed["file_idx"] if parsed else None,
        "name": states.clean_text(" ".join(str(stream.get(k) or "") for k in ("name", "title")).strip()
                                  or hints.get("filename") or "", 120) or "",
        "stream": stream,
    }


# --------------------------------------------------------------------------- against the request

def audio_ok(release, want):
    """Does the release give the audio the owner asked for?  `unknown` never counts as a dub."""
    wanted = want.get("audio") or "any"
    return wanted == "any" or release["audio"] == wanted


def tier_of(release, want):
    res_ok = want.get("resolution") is None or release["resolution"] == want["resolution"]
    a_ok = audio_ok(release, want)
    if res_ok and a_ok:
        return "exact"
    if a_ok:
        return "other_quality"
    if res_ok:
        return "other_audio"
    return "other_both"


def _quality_distance(release, want):
    """Closest lower resolution first, then higher, then 'not stated' (for other_quality)."""
    wanted, got = want.get("resolution"), release["resolution"]
    if wanted is None or got is None:
        return (2, 0)
    return (0, wanted - got) if got <= wanted else (1, got - wanted)


def score(release, want, batch_binge=None):
    """Within one tier.  Resolution match +40, log-scaled seeders up to +25, the batch's usual
    bingeGroup +10 (a consistent encode across a season), a small source/codec nudge, and a
    penalty when the file overshoots the size target (plan 9.3)."""
    total = 0.0
    if want.get("resolution") is not None and release["resolution"] == want["resolution"]:
        total += 40
    if release["seeders"]:
        total += min(25.0, 8.0 * math.log10(1 + release["seeders"]))
    if batch_binge and release["binge"] == batch_binge:
        total += 10
    total += {"remux": 3, "bluray": 3, "web-dl": 2, "webrip": 1}.get(release["source"], 0)
    if release["size"] and want.get("max_gb"):
        over = release["size"] / (want["max_gb"] * _GB)
        if over > 0.8:
            total -= 10 * (over - 0.8)
    return total


def label(release):
    """\"1080p dub\", \"720p sub\", \"1080p (language not stated)\", \"quality not stated\" ..."""
    res = "%dp" % release["resolution"] if release["resolution"] else "quality not stated"
    audio = {"dub": "dub", "sub": "sub"}.get(release["audio"], "language not stated")
    return "%s %s" % (res, audio) if release["audio"] != "unknown" else "%s, %s" % (res, audio)


def fmt_gb(size):
    return None if not size else round(size / _GB, 2)


def rejected_reason(release, want):
    """Why a release can never be picked (a hard filter), else None."""
    if release["banned"]:
        return "banned token %r" % release["banned"].upper()
    if want.get("max_gb") and release["size"] and release["size"] > want["max_gb"] * _GB:
        return "over %.1f GB" % want["max_gb"]
    if want.get("min_seeders") and release["seeders"] is not None and release["seeders"] < want["min_seeders"]:
        return "fewer than %d seeders" % want["min_seeders"]
    return None


def rank(releases, want, *, batch_binge=None, torrents_only=True):
    """[release] -> ({kept: [candidate], rejected: Counter of reasons}).  Candidates are the
    release dicts plus `tier`, `score` and `label`, best first (tier order, then quality distance
    inside other_quality, then score)."""
    kept, rejected = [], Counter()
    seen = set()
    for release in releases:
        if release is None:
            continue
        if torrents_only and not release["torrent"]:
            rejected["not a torrent stream (direct link)" if release["direct"] else "no torrent in it"] += 1
            continue
        why = rejected_reason(release, want)
        if why:
            rejected[why] += 1
            continue
        key = (release["infohash"], release["file_idx"]) if release["torrent"] else ("url", release["name"])
        if key in seen:                      # two addons offering the same torrent: one candidate
            continue
        seen.add(key)
        item = dict(release, tier=tier_of(release, want), score=score(release, want, batch_binge))
        item["label"] = label(item)
        kept.append(item)

    def order(c):
        distance = _quality_distance(c, want) if c["tier"] == "other_quality" else (0, 0)
        return (TIERS.index(c["tier"]), distance, -c["score"])
    kept.sort(key=order)
    return kept, rejected


def usual_binge(per_episode_top):
    """The most common bingeGroup among each episode's best candidates, or None."""
    count = Counter(c["binge"] for c in per_episode_top if c and c.get("binge"))
    if not count:
        return None
    group, hits = count.most_common(1)[0]
    return group if hits >= 2 else None


# --------------------------------------------------------------------------- one episode's answer

def _summary(candidate):
    return {"label": candidate["label"], "tier": candidate["tier"], "size_gb": fmt_gb(candidate["size"]),
            "seeders": candidate["seeders"], "name": candidate["name"]}


def choose(candidates, policy="ask"):
    """Pick for one episode from ranked `candidates`.

    Returns {status, pick, fallbacks, options}:
      status "exact"    the pick is what was asked for
             "fallback" nothing exact; the policy lets a lesser release through (pick says which)
             "pending"  nothing exact, policy `ask`: `options` lists what exists, one per tier
             "none"     nothing the policy allows (policy `skip`, or no candidates at all)
    `fallbacks` are the next releases the engine may switch to when the pick has no seeders; they
    are only ever releases the policy allows."""
    policy = policy if policy in POLICIES else "ask"
    allowed = POLICY_TIERS[policy]
    usable = [c for c in candidates if c["tier"] in allowed]
    if usable:
        pick = usable[0]
        rest = [c for c in usable[1:] if (c["infohash"], c["file_idx"]) != (pick["infohash"], pick["file_idx"])]
        return {"status": "exact" if pick["tier"] == "exact" else "fallback", "pick": pick,
                "fallbacks": rest[:MAX_FALLBACKS], "options": []}
    options = []
    if policy == "ask":
        for tier, policy_name in (("other_quality", "other_quality"), ("other_audio", "other_audio"), ("other_both", "best")):
            best = next((c for c in candidates if c["tier"] == tier), None)
            if best:
                options.append(dict(_summary(best), policy=policy_name))
    return {"status": "pending" if options else "none", "pick": None, "fallbacks": [], "options": options}
