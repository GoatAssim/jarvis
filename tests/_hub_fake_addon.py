"""A fake Stremio addon pair for the phase 6 tests (not a test itself).  Loopback, no network.

Two addons, because the owner has several and the fan-out must merge them:

    /cat   "Fake Catalog"  resources catalog + meta + stream; ids tt...; search catalog for series
    /str   "Fake Streams"  resources stream only; ids tt... and kitsu:

The one show is "Spirit Hunters" (tt7654321), two seasons, plus an anime-style "Moon Pilot"
catalogued by Kitsu id (kitsu:42, type "anime", episodes `kitsu:42:N`).

Spirit Hunters season 2 (the acceptance case: dub + 1080p):
    E1  dub 1080p (two addons offer it), sub 1080p, dub 720p
    E2  dub 1080p (two releases: the second is the engine's fallback), sub 720p, a CAM copy (banned)
    E3  NO dub 1080p: sub 1080p, dub 720p, a release that doesn't say its language
    E4  not released yet (future date)
Season 1 has three episodes with 720p subs only.  Every wording below is invented for the test;
it is NOT a record of what real addons write (see tests/addon_checks.py for that).

A third addon exists only when a test sets `state.cin`: "/cin" serves a RECORDED answer (the real Cinemeta,
tests/data/addon-answers-cinemeta.json) rebuilt into addon responses by test_hub_stremio_real_cinemeta.py.

`state.log` records every request path; `state.fail` = {"/str"} makes that addon answer 500 to everything, `state.fail_streams` only to stream requests;
`state.slow` = {"/str": seconds} delays it.

Added for brief 8D (all of it is off until a test sets it, so every older test sees the same behaviour as before):
`state.calls` = {"/str": n} counts every request an addon got and `state.stream_calls` only its stream requests;
`state.stream_delay` = {"/str": seconds} delays the addon's STREAM requests only (the manifest is not delayed);
`state.fail_mode` = {"/str": 404 | 500 | "timeout"} makes the addon's stream requests answer 404, answer 500, or hang for
`state.hang_s` seconds and then close the connection without an answer (the client sees "didn't answer").
"""

import hashlib
import http.server
import json
import threading
import time
from urllib.parse import unquote, urlsplit

SHOW = "tt7654321"
ANIME = "kitsu:42"


def infohash(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


class State:
    def __init__(self):
        self.log = []
        self.fail = set()
        self.fail_streams = set()
        self.slow = {}
        self.calls = {}          # brief 8D: requests per addon
        self.stream_calls = {}   # brief 8D: stream requests per addon
        self.stream_delay = {}   # brief 8D: seconds of delay for an addon's stream requests only
        self.fail_mode = {}      # brief 8D: {"/str": 404 | 500 | "timeout"} for stream requests
        self.hang_s = 1.0        # brief 8D: how long "timeout" hangs before the connection is closed
        self.posts = []
        self.movies = False      # True: "/cat" also has a searchable MOVIE catalog (two films: "Paper Lanterns", "Spirit Hunters: The Film")
        self.cin = None          # a RECORDED addon (test_hub_stremio_real_cinemeta.py) served at /cin: see Handler.cinemeta
        self.library = [
            {"_id": "tt0000011", "name": "Old Film", "type": "movie", "removed": False, "_mtime": "2026-01-01T00:00:00.000Z",
             "state": {"timesWatched": 0, "flaggedWatched": 0, "timeOffset": 600000, "duration": 6000000, "lastWatched": "2026-05-01T00:00:00.000Z"}},
            {"_id": SHOW, "name": "Spirit Hunters", "type": "series", "removed": False, "_mtime": "2026-02-01T00:00:00.000Z",
             "state": {"timesWatched": 3, "timeOffset": 0, "duration": 0, "lastWatched": "2026-06-01T00:00:00.000Z", "video_id": SHOW + ":2:1"}},
            {"_id": "tt0000012", "name": "Gone", "type": "movie", "removed": True, "state": {}},
        ]


STATE = State()


def _video(season, episode, released="2020-01-01"):
    return {"id": "%s:%d:%d" % (SHOW, season, episode), "season": season, "episode": episode,
            "title": "Episode %d" % episode, "released": released}


VIDEOS = ([_video(1, n) for n in (1, 2, 3)] + [_video(2, 1), _video(2, 2), _video(2, 3),
                                                _video(2, 4, released="2099-01-01")])
ANIME_VIDEOS = [{"id": "kitsu:42:%d" % n, "season": 1, "episode": n, "title": "Flight %d" % n, "released": "2021-01-01"}
                for n in (1, 2)]


def _stream(label, season, episode, desc, size_gb=1.4, seeders=50, binge="grp-a", idx=None, **extra):
    out = {"name": "Fake\n%s" % label, "title": "Spirit Hunters S%02dE%02d %s\n\U0001F464 %d" % (season, episode, desc, seeders),
           "infoHash": infohash(season, episode, label, desc),
           "behaviorHints": {"bingeGroup": binge, "videoSize": int(size_gb * 1024 ** 3),
                             "filename": "Spirit.Hunters.S%02dE%02d.%s.mkv" % (season, episode, desc.replace(" ", "."))}}
    if idx is not None:
        out["fileIdx"] = idx
    out.update(extra)
    return out


def streams_for(video_id, addon):
    parts = video_id.split(":")
    if parts[0] == "kitsu":
        return [_stream("1080p", 1, int(parts[-1]), "1080p Dual Audio", size_gb=1.0)] if addon == "/str" else []
    season, episode = int(parts[1]), int(parts[2])
    if season == 1:
        return [_stream("720p", 1, episode, "720p WEB-DL Eng Sub", size_gb=0.6)] if addon == "/cat" else []
    if addon == "/cat":
        table = {
            1: [_stream("1080p", 2, 1, "1080p WEB-DL DUAL AUDIO", seeders=120), _stream("1080p", 2, 1, "1080p WEB-DL Multi Sub", seeders=300),
                _stream("720p", 2, 1, "720p DUB", size_gb=0.7, seeders=40)],
            2: [_stream("1080p", 2, 2, "1080p BluRay Dubbed", seeders=80), _stream("720p", 2, 2, "720p Subbed", size_gb=0.7),
                _stream("CAM", 2, 2, "1080p CAM Dubbed", seeders=900),
                _stream("1080p", 2, 2, "1080p WEB-DL Dub x265", size_gb=0.9, seeders=20)],
            3: [_stream("1080p", 2, 3, "1080p WEB-DL Eng Sub", seeders=200), _stream("720p", 2, 3, "720p Dubbed", size_gb=0.7, seeders=30),
                _stream("1080p", 2, 3, "1080p WEB-DL x265", seeders=60)],
            4: [],
        }
        return table.get(episode, [])
    # the stream-only addon repeats E1's dub (same torrent: one candidate) and adds a direct link
    if episode == 1:
        return [_stream("1080p", 2, 1, "1080p WEB-DL DUAL AUDIO", seeders=120),
                {"name": "Direct", "title": "Spirit Hunters 1080p DUB", "url": "https://cdn.example.invalid/x/secret-token.mkv"}]
    return []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        STATE.log.append(path)
        addon = ("/cat" if path.startswith("/cat/") else "/str" if path.startswith("/str/")
                 else "/cin" if path.startswith("/cin/") and STATE.cin else None)
        if addon is None:
            return self._send(404, {})
        STATE.calls[addon] = STATE.calls.get(addon, 0) + 1
        if "/stream/" in path:
            STATE.stream_calls[addon] = STATE.stream_calls.get(addon, 0) + 1
        if addon in STATE.slow:
            time.sleep(STATE.slow[addon])
        if addon in STATE.fail:
            return self._send(500, {"err": "boom"})
        if "/stream/" in path:
            if STATE.stream_delay.get(addon):
                time.sleep(STATE.stream_delay[addon])
            mode = STATE.fail_mode.get(addon)
            if mode == 404:
                return self._send(404, {"err": "gone"})
            if mode == 500:
                return self._send(500, {"err": "boom"})
            if mode == "timeout":
                time.sleep(STATE.hang_s)
                self.close_connection = True
                return None
        rest = path[len(addon):]
        if addon == "/cin":
            return self.cinemeta(rest)
        if addon in STATE.fail_streams and "/stream/" in rest:
            return self._send(500, {"err": "boom"})
        if rest == "/manifest.json":
            return self._send(200, self.manifest(addon))
        pieces = (rest.strip("/")[:-5] if rest.endswith(".json") else rest.strip("/")).split("/")
        kind = pieces[0]
        if kind == "catalog" and len(pieces) == 4 and addon == "/cat" and pieces[1] == "movie":
            query = pieces[3].partition("=")[2].lower()          # the movie catalog answers films only (and only when STATE.movies)
            films = []
            if "lantern" in query or "paper" in query:
                films.append({"id": "tt7000100", "type": "movie", "name": "Paper Lanterns", "releaseInfo": "2022"})
            if "spirit" in query or "hunters" in query:
                films.append({"id": "tt7000200", "type": "movie", "name": "Spirit Hunters: The Film", "releaseInfo": "2023"})
            return self._send(200, {"metas": films})
        if kind == "catalog" and len(pieces) == 4 and addon == "/cat":
            query = pieces[3].partition("=")[2].lower()
            metas = []
            if "spirit" in query or "hunters" in query:
                metas.append({"id": SHOW, "type": "series", "name": "Spirit Hunters", "releaseInfo": "2019-", "poster": "https://img.invalid/sh.jpg"})
            if "spirit" in query:
                metas.append({"id": "tt7000001", "type": "series", "name": "Spirit Hunters: Origins", "releaseInfo": "2024"})
            if "moon" in query:
                metas.append({"id": ANIME, "type": "anime", "name": "Moon Pilot", "year": "2021"})
            if "twin" in query:
                metas += [{"id": "tt1", "type": "series", "name": "Twin Peaks A"}, {"id": "tt2", "type": "series", "name": "Twin Peaks B"}]
            return self._send(200, {"metas": metas})
        if kind == "meta" and addon == "/cat":
            item = pieces[2]
            if item == SHOW:
                return self._send(200, {"meta": {"id": SHOW, "type": "series", "name": "Spirit Hunters", "poster": "https://img.invalid/sh.jpg",
                                                  "background": "https://img.invalid/bg.jpg", "videos": VIDEOS, "year": "2019"}})
            if item == ANIME:
                return self._send(200, {"meta": {"id": ANIME, "type": "anime", "name": "Moon Pilot", "videos": ANIME_VIDEOS}})
            return self._send(200, {"meta": None})
        if kind == "stream":
            return self._send(200, {"streams": streams_for(pieces[2], addon)})
        return self._send(404, {})

    def cinemeta(self, rest):
        """/cin: an addon whose answers come from STATE.cin = {"manifest": {...}, "search": f(type, query) -> metas,
        "metas": {id: meta}}.  It has no stream resource, like the real Cinemeta."""
        cin = STATE.cin
        if rest == "/manifest.json":
            return self._send(200, cin["manifest"])
        pieces = (rest.strip("/")[:-5] if rest.endswith(".json") else rest.strip("/")).split("/")
        if pieces[0] == "catalog" and len(pieces) == 4:
            return self._send(200, {"metas": cin["search"](pieces[1], pieces[3].partition("=")[2].lower())})
        if pieces[0] == "meta" and len(pieces) == 3:
            return self._send(200, {"meta": cin["metas"].get(pieces[2])})
        return self._send(404, {})

    def do_POST(self):
        """The cloud API: POST /api/<method> with {"authKey": ...}."""
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        method = unquote(urlsplit(self.path).path).rsplit("/", 1)[-1]
        STATE.log.append("POST /api/" + method)
        STATE.posts.append((method, body))
        if body.get("authKey") != "SECRETKEY":
            return self._send(200, {"error": {"message": "session does not exist (key SECRETKEY)" if False else "bad session", "code": 1}})
        if method == "addonCollectionGet":
            base = "http://%s" % self.headers.get("Host")
            return self._send(200, {"result": {"addons": [
                {"transportUrl": base + "/cat/manifest.json", "manifest": self.manifest("/cat")},
                {"transportUrl": base + "/str/manifest.json", "manifest": self.manifest("/str")}]}})
        if method == "datastoreGet":
            items = [dict(x) for x in STATE.library]
            ids = body.get("ids") or []
            return self._send(200, {"result": [x for x in items if not ids or x["_id"] in ids]})
        if method == "datastorePut":
            for change in body.get("changes") or []:
                STATE.library = [change if x["_id"] == change["_id"] else x for x in STATE.library]
            return self._send(200, {"result": {"success": True}})
        return self._send(200, {"error": {"message": "unknown method"}})

    def manifest(self, addon):
        if addon == "/cat":
            catalogs = [{"type": "series", "id": "top", "extra": [{"name": "search"}]},
                        {"type": "anime", "id": "kitsu-top", "extra": [{"name": "search"}]},
                        {"type": "series", "id": "browse-only", "extra": [{"name": "genre", "isRequired": True}]}]
            if STATE.movies:
                catalogs.append({"type": "movie", "id": "movies-top", "extra": [{"name": "search"}]})
            return {"id": "fake.catalog", "name": "Fake Catalog", "version": "1.0.0", "types": ["series", "anime", "movie"],
                    "resources": ["catalog", "meta", {"name": "stream", "types": ["series"], "idPrefixes": ["tt"]}],
                    "idPrefixes": ["tt", "kitsu:"], "catalogs": catalogs}
        return {"id": "fake.streams", "name": "Fake Streams", "version": "2.1.0", "types": ["series", "anime"],
                "resources": ["stream"], "idPrefixes": ["tt", "kitsu:"], "catalogs": []}


def start():
    """-> (base url, state, server).  Addon URLs are base + '/cat/manifest.json' and base + '/str/manifest.json'."""
    STATE.__init__()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_address[1], STATE, server
