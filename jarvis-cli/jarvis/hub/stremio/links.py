"""Pure helpers: no network, no files.  Everything the Stremio engine, the tools and the router
need to understand a magnet, a Stremio stream, a server URL or a pack's file list.

The hub stores ONE canonical text per Stremio download (`source_url`):

    magnet:?xt=urn:btih:<40 hex>[&so=<file index>][&dn=<name>][&tr=<tracker>...]

A bare infohash, a magnet in base32, a Stremio stream object (`{"infoHash": ..., "fileIdx": ...}`)
and `http://127.0.0.1:11470/<hash>/<idx>` all normalise to it, so the dedupe key
(`identity`: infohash + file index) is the same whichever way the owner pasted it.  Trackers and
the display name are carried along but never part of the identity.
"""

import base64
import json
import os
import re
import zlib
from urllib.parse import parse_qs, quote, unquote, urlsplit

_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_BASE32 = re.compile(r"^[A-Za-z2-7]{32}$")
IMDB_RE = re.compile(r"^tt\d{5,10}$")
MAX_TRACKERS = 20
MAX_TRACKER_LEN = 500
MAX_NAME = 160
_TRACKER_SCHEMES = ("udp://", "http://", "https://", "ws://", "wss://")
MAX_FILE_INDEX = 100000

VIDEO_EXT = frozenset(".mp4 .mkv .avi .mov .webm .m4v .wmv .flv .ts .mpg .mpeg .m2ts".split())


# --------------------------------------------------------------------------- infohashes

def normalize_infohash(text):
    """40-hex (any case) or 32-char base32 -> lower-case 40-hex, else None."""
    value = str(text or "").strip()
    if _HEX40.match(value):
        return value.lower()
    if _BASE32.match(value):
        try:
            return base64.b32decode(value.upper()).hex()
        except (ValueError, TypeError):
            return None
    return None


def valid_infohash(text):
    return normalize_infohash(text) is not None


def _file_index(value):
    """A file index from a stream / magnet: an int >= 0, else None (-1 and junk mean 'pick')."""
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= MAX_FILE_INDEX else None


def _clean_trackers(values):
    out = []
    for raw in values or []:
        text = str(raw or "").strip()
        if text.lower().startswith("tracker:"):
            text = text[len("tracker:"):]
        if not text.lower().startswith(_TRACKER_SCHEMES) or len(text) > MAX_TRACKER_LEN:
            continue
        if any(ch in text for ch in " \r\n\t\"'<>"):
            continue
        if text not in out:
            out.append(text)
        if len(out) >= MAX_TRACKERS:
            break
    return out


def _clean_name(text):
    value = " ".join(str(text or "").split())
    return value[:MAX_NAME]


# --------------------------------------------------------------------------- canonical text

def build_magnet(infohash, file_idx=None, name=None, trackers=()):
    """The canonical source text.  `infohash` must already be valid."""
    digest = normalize_infohash(infohash)
    if not digest:
        raise ValueError("not an infohash")
    out = "magnet:?xt=urn:btih:%s" % digest
    idx = _file_index(file_idx)
    if idx is not None:
        out += "&so=%d" % idx
    if name:
        out += "&dn=%s" % quote(_clean_name(name), safe="")
    for tracker in _clean_trackers(trackers):
        out += "&tr=%s" % quote(tracker, safe="")
    return out


def parse_magnet(text):
    """magnet:?xt=urn:btih:... -> {infohash, file_idx, name, trackers} or None."""
    value = str(text or "").strip()
    if not value.lower().startswith("magnet:?"):
        return None
    try:
        query = parse_qs(urlsplit(value).query, keep_blank_values=True)
    except ValueError:
        return None
    digest = None
    for xt in query.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            digest = normalize_infohash(xt[len("urn:btih:"):])
            if digest:
                break
    if not digest:
        return None
    idx = None
    for so in query.get("so", []):
        if re.match(r"^\d+$", so.strip()):          # "so=0,2-4" selects several: leave the pick to us
            idx = _file_index(so)
            break
    names = query.get("dn", [])
    return {"infohash": digest, "file_idx": idx, "name": _clean_name(names[0]) if names else "",
            "trackers": _clean_trackers(query.get("tr", []))}


def parse_stream(obj):
    """A Stremio stream object (dict or its JSON text) -> {infohash, file_idx, name, trackers,
    filename, video_size} or None when it isn't a torrent stream.

    A real stream object has `infoHash` (and `fileIdx`).  A `stream_search` ROW is accepted too (N26): one
    that has a `magnet` (its hash, file index, name and trackers are read from it) or an `infohash` and a
    `file_idx`, and no `infoHash`.  For an object that has `infoHash` nothing here changed."""
    if isinstance(obj, str):
        text = obj.strip()
        if not text.startswith("{") or len(text) > 8192:
            return None
        try:
            obj = json.loads(text)
        except ValueError:
            return None
    if not isinstance(obj, dict):
        return None
    digest = normalize_infohash(obj.get("infoHash") or obj.get("infohash"))
    row = not normalize_infohash(obj.get("infoHash"))          # a search row, not a stream object
    magnet = None
    if not digest and isinstance(obj.get("magnet"), str):
        magnet = parse_magnet(obj["magnet"])
        digest = magnet["infohash"] if magnet else None
    if not digest:
        return None
    trackers = []
    for source in obj.get("sources") or []:
        if isinstance(source, str) and source.lower().startswith("tracker:"):
            trackers.append(source)
    if magnet:
        trackers += magnet["trackers"]
    hints = obj.get("behaviorHints") if isinstance(obj.get("behaviorHints"), dict) else {}
    size = hints.get("videoSize")
    file_idx = _file_index(obj.get("fileIdx"))
    if file_idx is None and row:
        file_idx = _file_index(obj.get("file_idx"))
        if file_idx is None and magnet:
            file_idx = magnet["file_idx"]
    return {"infohash": digest, "file_idx": file_idx,
            "name": _clean_name(hints.get("filename") or obj.get("title") or obj.get("name") or (magnet or {}).get("name") or ""),
            "trackers": _clean_trackers(trackers),
            "filename": _clean_name(hints.get("filename") or ""),
            "video_size": size if isinstance(size, int) and size > 0 else None}


def parse_server_url(text, server_hostport):
    """http://127.0.0.1:11470/<hash>/<idx or -1>[/...][?...] -> {infohash, file_idx} when the host
    is THE configured streaming server, else None.  The filename form
    (/<hash>/<name>?download=1) carries no index and gives {infohash, file_idx: None}."""
    value = str(text or "").strip()
    try:
        parts = urlsplit(value)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    want = str(server_hostport or "").lower()
    if "%s:%s" % (host, port or (443 if parts.scheme.lower() == "https" else 80)) != want:
        return None
    pieces = [p for p in parts.path.split("/") if p]
    if not pieces:
        return None
    digest = normalize_infohash(pieces[0])
    if not digest or len(pieces[0]) != 40:
        return None
    idx = _file_index(pieces[1]) if len(pieces) > 1 else None
    return {"infohash": digest, "file_idx": idx}


def is_torrent_url(text):
    """An http(s) link to a .torrent file (the engine fetches it and registers the bytes)."""
    value = str(text or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname) \
        and parts.path.lower().endswith(".torrent") and len(value) <= 2048


def parse_input(text, server_hostport=None):
    """What does the owner's text mean to the Stremio engine?

    Returns {"kind": "magnet" | "infohash" | "stream" | "server_url" | "torrent_url", ...} plus the
    parsed fields, or None when it isn't ours.  Never raises."""
    value = str(text or "").strip()
    if not value or len(value) > 4096:
        return None
    if value.lower().startswith("magnet:?"):
        parsed = parse_magnet(value)
        return dict(parsed, kind="magnet") if parsed else None
    if normalize_infohash(value) and " " not in value and len(value) in (40, 32):
        # a bare 32-letter word is ambiguous (it could be base32); only 40-hex counts as a bare hash
        if len(value) == 40:
            return {"kind": "infohash", "infohash": normalize_infohash(value), "file_idx": None,
                    "name": "", "trackers": []}
        return None
    if value.startswith("{"):
        parsed = parse_stream(value)
        return dict(parsed, kind="stream") if parsed else None
    if server_hostport:
        parsed = parse_server_url(value, server_hostport)
        if parsed:
            return dict(parsed, kind="server_url", name="", trackers=[])
    if is_torrent_url(value):
        return {"kind": "torrent_url", "url": value, "infohash": None, "file_idx": None,
                "name": "", "trackers": []}
    return None


def canonical_source(parsed):
    """The text stored as `source_url` for a parsed input."""
    if parsed.get("kind") == "torrent_url":
        return parsed["url"]
    return build_magnet(parsed["infohash"], parsed.get("file_idx"), parsed.get("name"), parsed.get("trackers"))


def identity(text):
    """The dedupe identity of a Stremio source: 'btih:<hash>' or 'btih:<hash>:<idx>'.  None when
    the text is not a magnet (a .torrent URL is deduped on its URL like any other link)."""
    parsed = parse_magnet(text)
    if not parsed:
        return None
    out = "btih:%s" % parsed["infohash"]
    if parsed["file_idx"] is not None:
        out += ":%d" % parsed["file_idx"]
    return out


def identity_magnet(text):
    """A magnet stripped to hash + file index: what `router.canonical_url` compares, so two
    pastes of the same torrent with different trackers are ONE download."""
    parsed = parse_magnet(text)
    if not parsed:
        return None
    return build_magnet(parsed["infohash"], parsed["file_idx"])


# --------------------------------------------------------------------------- the server's URLs

def download_url(server_url, infohash, file_idx):
    """Stremio's own 'Download video' URL (it answers 307 -> /<hash>/<name>?download=1)."""
    digest = normalize_infohash(infohash)
    if not digest:
        raise ValueError("not an infohash")
    idx = _file_index(file_idx)
    return "%s/%s/%d?external=1&download=1" % (server_url.rstrip("/"), digest, -1 if idx is None else idx)


def stream_url(server_url, infohash, file_idx):
    digest = normalize_infohash(infohash)
    if not digest:
        raise ValueError("not an infohash")
    idx = _file_index(file_idx)
    return "%s/%s/%d" % (server_url.rstrip("/"), digest, -1 if idx is None else idx)


def create_body(infohash, trackers=()):
    """The body stremio-core sends to POST /<hash>/create."""
    digest = normalize_infohash(infohash)
    sources = ["dht:%s" % digest] + ["tracker:%s" % t for t in _clean_trackers(trackers)]
    return {"peerSearch": {"min": 40, "max": 200, "sources": sources}}


# --------------------------------------------------------------------------- picking a file

_EPISODE_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})(?!\d)"),
    re.compile(r"(?<![0-9A-Za-z])(\d{1,2})[xX](\d{2,3})(?![0-9A-Za-z])"),
    re.compile(r"[Ss]eason[ ._-]*(\d{1,2})[ ._-]*[Ee]pisode[ ._-]*(\d{1,3})"),
)


def episode_of(name):
    """(season, episode) from a file name / path, or None."""
    text = str(name or "")
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _is_video(entry):
    return os.path.splitext(str(entry.get("path") or entry.get("name") or ""))[1].lower() in VIDEO_EXT


def _length(entry):
    try:
        return max(0, int(entry.get("length") or 0))
    except (TypeError, ValueError):
        return 0


def _is_sample(entry):
    return "sample" in str(entry.get("path") or entry.get("name") or "").lower()


def pick_file(files, file_idx=None, season=None, episode=None):
    """Which file of a torrent?  Returns (index, why) or (None, why-not).

    Order: the index the addon / owner gave (when it exists); else the file whose name says
    SxxExx for the wanted episode (a pack); else the largest video file; else the largest file
    (what the server's own `-1` does).  A sample clip never wins over a real file."""
    entries = [f for f in (files or []) if isinstance(f, dict)]
    if not entries:
        return None, "the torrent has no file list yet"
    idx = _file_index(file_idx)
    if idx is not None:
        if idx < len(entries) and _length(entries[idx]) > 0:
            return idx, "the file the source named"
        return None, "the source names file %d but the torrent has %d file(s)" % (idx, len(entries))
    ranked = [(i, f) for i, f in enumerate(entries) if _length(f) > 0]
    if not ranked:
        return None, "every file in the torrent is empty"
    if season is not None and episode is not None:
        want = (int(season), int(episode))
        hits = [(i, f) for i, f in ranked if episode_of(f.get("path") or f.get("name")) == want and _is_video(f)]
        hits = [h for h in hits if not _is_sample(h[1])] or hits
        if hits:
            return max(hits, key=lambda h: _length(h[1]))[0], "matched S%02dE%02d" % want
        real = [(i, f) for i, f in ranked if _is_video(f) and not _is_sample(f)]
        if len(real) == 1:                     # a single-episode torrent rarely says SxxExx in its name
            return real[0][0], "the only video file"
        return None, "no file in the torrent looks like S%02dE%02d" % want
    videos = [(i, f) for i, f in ranked if _is_video(f) and not _is_sample(f)] \
        or [(i, f) for i, f in ranked if _is_video(f)]
    pool = videos or ranked
    return max(pool, key=lambda h: _length(h[1]))[0], "the largest %s" % ("video file" if videos else "file")


def file_name(entry):
    """The bare file name of a stats.json file entry (never a path)."""
    raw = str(entry.get("name") or entry.get("path") or "")
    return raw.replace("\\", "/").split("/")[-1]


# --------------------------------------------------------------------------- stremio-web links

def encode_stream(stream):
    """JSON -> zlib (level 0) -> base64 -> percent-encode: how stremio-web puts a stream in a URL."""
    raw = json.dumps(stream, separators=(",", ":")).encode("utf-8")
    return quote(base64.b64encode(zlib.compress(raw, 0)).decode("ascii"), safe="")


def decode_stream(text):
    """The inverse of encode_stream (used by the tests and by anything that reads a player link)."""
    data = zlib.decompress(base64.b64decode(unquote(str(text))))
    return json.loads(data.decode("utf-8"))


def _imdb(imdb_id):
    value = str(imdb_id or "").strip().lower()
    if not IMDB_RE.match(value):
        raise ValueError("that isn't an IMDb id (expected something like tt0944947)")
    return value


def _num(value, label, low=0, high=9999):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number" % label)
    if not low <= number <= high:
        raise ValueError("%s is out of range" % label)
    return number


def video_id(imdb_id, season, episode):
    return "%s:%d:%d" % (_imdb(imdb_id), _num(season, "season"), _num(episode, "episode", 0, 99999))


def deep_link_detail(web_url, imdb_id, kind="series", season=None, episode=None):
    """#/detail/series/<id>[/<id>:<S>:<E>] or #/detail/movie/<id>/<id>."""
    base = str(web_url).rstrip("/")
    imdb = _imdb(imdb_id)
    if kind == "movie":
        return "%s/#/detail/movie/%s/%s" % (base, imdb, imdb)
    if kind != "series":
        raise ValueError("type must be series or movie")
    if season is None and episode is None:
        return "%s/#/detail/series/%s" % (base, imdb)
    vid = video_id(imdb, season, episode)
    return "%s/#/detail/series/%s/%s" % (base, imdb, quote(vid, safe=":"))


NATIVE_SCHEME = "stremio:///"


def native_link(link, web_url):
    """The Stremio app's own form of a link this package built on the web address: `<web>/#/detail/series/tt1/tt1:1:1`
    becomes `stremio:///detail/series/tt1/tt1:1:1` (the app registers the stremio:// scheme and shows the same screens).
    Raises ValueError for anything that is not a `<web address>/#/...` link, so only links this package built get through."""
    base = str(web_url).rstrip("/")
    text = str(link)
    if not text.startswith(base + "/#/"):
        raise ValueError("that isn't a link on the stremio-web address")
    return NATIVE_SCHEME + text[len(base) + 3:]


_SCREENS = {"board": "#/", "discover": "#/discover", "library": "#/library", "addons": "#/addons",
            "settings": "#/settings", "calendar": "#/calendar"}


def screen_link(web_url, screen, query=None):
    base = str(web_url).rstrip("/")
    screen = str(screen or "").strip().lower()
    if screen == "search":
        text = " ".join(str(query or "").split())[:200]
        if not text:
            raise ValueError("search needs some text")
        return "%s/#/search?search=%s" % (base, quote(text, safe=""))
    if screen not in _SCREENS:
        raise ValueError("unknown screen %r (use one of: %s, search, detail)" % (screen[:30], ", ".join(sorted(_SCREENS))))
    return base + "/" + _SCREENS[screen]


def player_link(web_url, stream, imdb_id, season, episode, meta_transport_url, stream_transport_url=None,
                kind="series"):
    """#/player/<stream>/<streamTransportUrl>/<metaTransportUrl>/<type>/<id>/<videoId>.

    NOT confirmed against a live stremio-web (plan 9.4 gives the shape): the caller treats this
    tier as best-effort and falls back to the deep link."""
    imdb = _imdb(imdb_id)
    vid = video_id(imdb, season, episode) if kind == "series" else imdb
    meta = quote(str(meta_transport_url), safe="")
    source = quote(str(stream_transport_url or meta_transport_url), safe="")
    return "%s/#/player/%s/%s/%s/%s/%s/%s" % (str(web_url).rstrip("/"), encode_stream(stream), source, meta,
                                              kind, imdb, quote(vid, safe=""))
