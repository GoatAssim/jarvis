"""yt-dlp playlists as ONE GROUP, and the generic extractor asked once (Download Hub, brief 21).

This is the logic behind two arguments of `dl_add_ytdlp`.  It lives here, not in the tool file,
because `dl_add_ytdlp` itself (`actions/download_hub.py`) is another agent's file this round: that
file only needs the small hook described at the end of this docstring.  The module starts with an
underscore on purpose: engines/__init__.py skips it, so it is never mistaken for an engine.

PLAYLISTS.  A hub download is ONE video; the `playlist` option of `ytdl_download` is never passed
to yt-dlp.  A playlist becomes a group of single-video rows instead:

  * the owner says yes first.  A link that is a playlist (or a channel) and arrives with no
    `playlist` argument queues NOTHING: the answer has the playlist's title, its size, the first
    five titles and the exact call to make to take them;
  * `playlist="all"` looks at the playlist (a probe in the worker: flat extraction, no media, at
    most 100 entries, a time limit), then queues up to PLAYLIST_CAP (50) videos as one group named
    after the playlist, each with the same video options;
  * a link that names a video AND a playlist (`watch?v=X&list=Y`) is the video, as it always was,
    with a note saying how to take the playlist;
  * the answer never lists more than PREVIEW_TITLES titles: the number of entries is
    model-visible, so it is a count and a few names, not a hundred lines.

GENERIC.  A link no specific yt-dlp extractor claims gets one plain answer.  `generic=true` lets
yt-dlp's generic extractor try THAT link, once; the row remembers it (`options.generic`), so a
retry never asks again.  It only works when ytdlp.GENERIC_ENABLED is True (it is not, in this
version), and then only for an http(s) link whose host resolves to public addresses.

THE HOOK for `tool_dl_add_ytdlp` in actions/download_hub.py (after `video = ...`):

    from ..hub.engines import _ytdlp_playlist
    return _ytdlp_playlist.tool(args, urls, video, _queue)

and the two schema properties, `_ytdlp_playlist.TOOL_PROPS`, added to dl_add_ytdlp's.
"""

import importlib.util
import re
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from .. import states
from . import ytdlp as yt

PLAYLIST_CAP = 50              # the most rows one call may queue (a setting may only LOWER it)
PROBE_MAX_ENTRIES = 100        # what the probe looks at
PROBE_TIMEOUT_S = 60.0
PREVIEW_TITLES = 5
ADDED_SHOWN = 5                # rows of `added` / `skipped` the answer carries

TOOL_PROPS = {
    "playlist": {"type": "string", "enum": ["one", "all"],
                 "description": ("A playlist or channel link is never taken silently. 'all' = look, then queue up "
                                 "to 50 of its videos as one group. 'one' = just the single video the link names. "
                                 "Without it a playlist link queues nothing and asks.")},
    "generic": {"type": "boolean",
                "description": ("Only after the owner said yes for THIS link: let yt-dlp's generic extractor try a "
                                "page no site-specific extractor knows. One link per call. Default false.")},
}

_YT_HOSTS = frozenset(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                       "youtu.be", "www.youtu.be"])
_YT_CHANNEL = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(/(videos|streams|shorts|playlists|featured|releases))?/?$")
_NAME_HINTS = ("playlist", ":tab", ":set", "album", "channel", "series", ":list", "user")


# --------------------------------------------------------------------------- what a link is

def parse_playlist_arg(value):
    """None | "one" | "all", and an error text."""
    if value is None or value == "":
        return None, None
    text = str(value).strip().lower()
    if text in ("one", "all"):
        return text, None
    return None, "playlist must be 'one' or 'all'"


def link_kind(url):
    """'video', 'mixed' (a video inside a playlist: watch?v=X&list=Y) or 'playlist' (a playlist or
    a channel page).  Judged from the link alone, no network.  YouTube is recognised by its
    address; another site only by the NAME of the yt-dlp extractor that claims it."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return "video"
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    query = parse_qs(parts.query)
    if host in _YT_HOSTS:
        has_list = bool((query.get("list") or [""])[0])
        if host.endswith("youtu.be"):
            return "mixed" if (has_list and path.strip("/")) else "video"
        if path == "/watch":
            if query.get("v"):
                return "mixed" if has_list else "video"
            return "playlist" if has_list else "video"
        if path == "/playlist" or path == "/results" or _YT_CHANNEL.match(path):
            return "playlist"
        return "video"
    name = (yt._claimed_by(str(url).strip()) or "").lower()
    if name and any(hint in name for hint in _NAME_HINTS):
        return "playlist"
    return "video"


def playlist_url(url):
    """The link to LOOK AT for a playlist: a watch?v=X&list=Y link becomes the playlist's own page."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return str(url).strip()
    if (parts.hostname or "").lower() in _YT_HOSTS:
        ident = (parse_qs(parts.query).get("list") or [""])[0]
        if ident and link_kind(url) == "mixed":
            return "https://www.youtube.com/playlist?list=" + ident
    return str(url).strip()


def effective_cap(settings):
    """PLAYLIST_CAP, or a lower number the owner set (`ytdlp.playlist_max`).  Never higher."""
    try:
        value = int(((settings or {}).get("ytdlp") or {}).get("playlist_max"))
    except (TypeError, ValueError):
        return PLAYLIST_CAP
    return max(1, min(PLAYLIST_CAP, value))


# --------------------------------------------------------------------------- the probe

def probe_playlist(url, timeout=None, max_entries=None):
    """Look at a playlist without downloading it.  Returns the worker's answer, or
    {"ok": False, "error": ...}.  Never raises."""
    limit = PROBE_TIMEOUT_S if timeout is None else float(timeout)
    cap = max(1, min(int(max_entries or PROBE_MAX_ENTRIES), PROBE_MAX_ENTRIES))
    try:
        proc = yt._spawn({"op": "probe", "url": str(url), "max_entries": cap}, tempfile.gettempdir())
    except OSError as exc:
        return {"ok": False, "error": "couldn't start the yt-dlp helper: %s" % yt._short(exc)}
    run = yt._Run(proc)
    end = time.monotonic() + limit
    while run.running() and time.monotonic() < end:
        time.sleep(0.05)
    if run.running():
        run.kill()
        return {"ok": False, "error": "looking at the playlist took longer than %d seconds" % int(limit)}
    run.finish()
    result = run.result
    if not isinstance(result, dict):
        tail = states.clean_text(run.stderr_tail.strip().splitlines()[-1] if run.stderr_tail.strip() else "", 160)
        return {"ok": False, "error": "the yt-dlp helper stopped unexpectedly%s" % (": " + tail if tail else "")}
    if not result.get("ok"):
        return {"ok": False, "error": states.clean_text(result.get("error"), 300) or "yt-dlp failed"}
    return clean_probe(result, cap)


def clean_probe(result, cap=PROBE_MAX_ENTRIES):
    """Never trust the pipe: bounded, typed, http(s) links only."""
    if not result.get("is_playlist"):
        return {"ok": True, "is_playlist": False, "title": states.clean_text(result.get("title"), 120)}
    entries, seen = [], set()
    for entry in (result.get("entries") or [])[:cap]:
        if not isinstance(entry, dict):
            continue
        link = str(entry.get("url") or "").strip()
        if not re.match(r"^https?://", link, re.I) or len(link) > 2048 or link in seen:
            continue
        seen.add(link)
        entries.append({"title": states.clean_text(entry.get("title"), 120), "url": link})

    def whole(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    count = max(whole(result.get("count")), len(entries))
    return {"ok": True, "is_playlist": True, "title": states.clean_text(result.get("title"), 120) or "Playlist",
            "count": count, "more": bool(result.get("more")), "entries": entries,
            "unavailable": whole(result.get("unavailable")), "unusable": whole(result.get("unusable"))}


# --------------------------------------------------------------------------- the tool

def _svc():
    from .. import service
    return service


def _call(url, **extra):
    arguments = {"url": url}
    arguments.update(extra)
    return {"tool": "dl_add_ytdlp", "arguments": arguments}


def _size_words(probe):
    count = probe.get("count") or len(probe.get("entries") or [])
    return "%s%d video%s" % ("at least " if probe.get("more") else "", count, "" if count == 1 else "s")


def _unclaimed(urls):
    """The plain answer for links no specific yt-dlp extractor claims (nothing is queued)."""
    hosts = []
    for url in urls:
        try:
            host = urlsplit(url).hostname or url[:60]
        except ValueError:
            host = url[:60]
        if host not in hosts:
            hosts.append(host)
    shown = ", ".join(hosts[:3])
    out = {"ok": False, "needs_clarification": True, "queued": 0, "unsupported": [u[:120] for u in urls[:5]],
           "generic_available": bool(yt.GENERIC_ENABLED)}
    if yt.GENERIC_ENABLED:
        out["message"] = ("yt-dlp has no specific support for %s. Its generic extractor can try to find a video on "
                          "such a page, but that is a guess, so it needs the owner's yes first, one link at a time. "
                          "Nothing was queued. If the owner agrees, call dl_add_ytdlp again with generic=true and "
                          "that one url." % shown)
        out["call"] = _call(urls[0], generic=True)
    else:
        out["message"] = ("yt-dlp has no specific support for %s. Its generic extractor could try, but that is "
                          "switched off in this version, so nothing was queued. A direct link to a media file "
                          "goes to dl_add." % shown)
    return out


def _is_unclaimed(decision, url=""):
    """True for a PAGE no specific extractor claims.  A link to a file is not one: it keeps the old
    plain refusal ("the ytdlp engine can't take that input"), and dl_add takes it."""
    return (not decision.get("ok") and decision.get("engine") == "ytdlp"
            and str(decision.get("problem") or "").endswith("can't take that input")
            and not yt._looks_like_file(url))


def _trim(res):
    """The answer carries counts and a few rows, not fifty."""
    if not isinstance(res, dict):
        return res
    for key in ("added", "skipped"):
        rows = res.get(key)
        if isinstance(rows, list) and len(rows) > ADDED_SHOWN:
            res[key] = rows[:ADDED_SHOWN]
            res[key + "_not_shown"] = len(rows) - ADDED_SHOWN
    return res


def _note(res, text):
    if isinstance(res, dict):
        res["note"] = (res["note"] + " " + text) if res.get("note") else text
    return res


def _ask(url, probe):
    titles = [e["title"] or "(untitled)" for e in probe["entries"][:PREVIEW_TITLES]]
    size = _size_words(probe)
    take = min(PLAYLIST_CAP, probe.get("count") or len(probe["entries"]))
    return {"ok": False, "needs_clarification": True, "queued": 0,
            "playlist": {"title": probe["title"], "count": probe.get("count"), "more": probe.get("more", False),
                         "first_titles": titles, "cap": PLAYLIST_CAP},
            "message": ("That link is a playlist, \"%s\", with %s. Nothing was queued. Ask the owner whether to take "
                        "them: call dl_add_ytdlp again with the same url and playlist=\"all\" to queue the first %d "
                        "(at most %d per call) as one group. For one video, give that video's own link."
                        % (probe["title"], size, take, PLAYLIST_CAP)),
            "call": _call(url, playlist="all")}


def _take(args, url, video, queue):
    """playlist='all': look, then queue up to the cap as one group."""
    svc = _svc()
    decision = svc.preview(url, engine="ytdlp")
    if _is_unclaimed(decision, url):
        return _unclaimed([url])
    if not decision.get("ok"):
        return {"ok": False, "error": decision.get("problem") or "yt-dlp can't take that link", "queued": 0}
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "would": "look at the playlist, then queue up to %d of its videos "
                "as one group (nothing was looked at or queued)" % PLAYLIST_CAP, "kind": link_kind(url)}
    probe = probe_playlist(playlist_url(url))
    if not probe.get("ok"):
        return {"ok": False, "queued": 0, "error": "couldn't look into that playlist, so nothing was queued: %s"
                % probe.get("error")}
    if not probe.get("is_playlist"):
        res = queue(args, [url], "ytdlp", video, "dl_add_ytdlp")
        return _note(res, "That link is one video, not a playlist; it was queued as one.")
    entries = probe["entries"]
    if not entries:
        return {"ok": False, "queued": 0, "error": "that playlist has no video I can queue (all private, "
                "removed or not understood)"}
    settings = (svc.get_settings().get("settings") or {})
    cap = effective_cap(settings)
    chosen = entries[:cap]
    args2 = dict(args)
    args2["group"] = (str(args.get("group") or "").strip() or probe["title"])[:80]
    res = queue(args2, [e["url"] for e in chosen], "ytdlp", video, "dl_add_ytdlp")
    if not isinstance(res, dict):
        return res
    total = max(probe.get("count") or 0, len(entries))
    not_taken = max(0, total - len(chosen))
    res["playlist"] = {"title": probe["title"], "count": total, "more": probe.get("more", False),
                       "queued": res.get("count", 0), "not_taken": not_taken, "cap": cap,
                       "unavailable": probe.get("unavailable", 0)}
    if res.get("added") or res.get("count"):
        words = ("Queued %d of %s from \"%s\" as one group." % (res.get("count", 0), _size_words(probe), probe["title"]))
        if not_taken:
            words += " %s%d were not taken: the limit is %d per call." % ("At least " if probe.get("more") else "",
                                                                          not_taken, cap)
        _note(res, words)
    return _trim(res)


def _generic(args, urls, video, queue, mode):
    if mode == "all":
        return {"ok": False, "queued": 0, "error": "generic can't be combined with playlist='all': the generic "
                "extractor takes one page at a time"}
    if not yt.GENERIC_ENABLED:
        return {"ok": False, "queued": 0, "generic_available": False,
                "error": "yt-dlp's generic extractor is switched off in this version, so nothing was queued"}
    if len(urls) != 1:
        return {"ok": False, "queued": 0, "error": "generic=true takes exactly one link per call (the owner's yes "
                "is for one link)"}
    url = urls[0]
    from .. import router
    svc = _svc()
    if svc.preview(url, engine="ytdlp").get("ok"):          # a specific extractor has it after all
        return _note(queue(args, [url], "ytdlp", video, "dl_add_ytdlp"),
                     "A site-specific extractor handles that link, so the generic one was not needed.")
    why = yt.generic_problem(url)
    if why:
        return {"ok": False, "queued": 0, "error": "the generic extractor can't take that link: %s" % why}
    with router.generic_allowed([url]):
        return queue(args, [url], "ytdlp", dict(video, generic=True), "dl_add_ytdlp")


def tool(args, urls, video, queue):
    """The playlist / generic part of `dl_add_ytdlp`.  `queue` is the tool file's own `_queue`
    (args, urls, engine, engine_options, label).  Returns the tool's answer."""
    args = args or {}
    mode, problem = parse_playlist_arg(args.get("playlist"))
    if problem:
        return {"ok": False, "error": problem}
    if args.get("generic") is True:
        return _generic(args, urls, video, queue, mode)
    if mode == "all":
        if len(urls) != 1:
            return {"ok": False, "queued": 0, "error": "playlist='all' takes one playlist link per call"}
        return _take(args, urls[0], video, queue)

    svc = _svc()
    kinds = {u: link_kind(u) for u in urls}
    pure = [u for u in urls if kinds[u] == "playlist"]
    if pure and not args.get("dry_run"):
        if mode == "one":
            return {"ok": False, "needs_clarification": True, "queued": 0,
                    "message": "That link is a playlist or a channel, not one video, so 'one' has nothing to take. "
                               "Give one video's own link, or call again with playlist=\"all\"."}
        if len(urls) > 1:
            return {"ok": False, "needs_clarification": True, "queued": 0, "playlist_links": [u[:120] for u in pure[:5]],
                    "message": "%d of those links are playlists or channels. Nothing was queued. Queue each "
                               "playlist on its own with playlist=\"all\", or leave them out." % len(pure)}
        decision = svc.preview(urls[0], engine="ytdlp")
        if not decision.get("ok"):
            return _unclaimed(urls) if _is_unclaimed(decision, urls[0]) else \
                {"ok": False, "queued": 0, "error": decision.get("problem") or "yt-dlp can't take that link"}
        probe = probe_playlist(playlist_url(urls[0]))
        if not probe.get("ok"):
            return {"ok": False, "queued": 0, "error": "that link is a playlist and I couldn't look into it, so "
                    "nothing was queued: %s" % probe.get("error")}
        if probe.get("is_playlist"):
            return _ask(urls[0], probe)
        # a page that only LOOKED like a playlist: one video after all, queue it as always

    claimed, unclaimed = [], []
    for url in urls:
        decision = svc.preview(url, engine="ytdlp")
        (unclaimed if _is_unclaimed(decision, url) else claimed).append(url)
    if not claimed:
        return _unclaimed(unclaimed)
    res = queue(args, claimed, "ytdlp", video, "dl_add_ytdlp")
    if isinstance(res, dict):
        if unclaimed:
            res["unsupported"] = [u[:120] for u in unclaimed[:5]]
            _note(res, _unclaimed(unclaimed)["message"])
        mixed = [u for u in claimed if kinds.get(u) == "mixed"]
        if mixed and mode is None and not res.get("dry_run"):
            _note(res, "%s also name%s a playlist; only the video was queued. To take the whole playlist (up to %d, "
                       "as one group), call dl_add_ytdlp with that url and playlist=\"all\"."
                  % ("That link" if len(mixed) == 1 else "%d of those links" % len(mixed),
                     "s" if len(mixed) == 1 else "", PLAYLIST_CAP))
        if pure and args.get("dry_run"):
            res["note"] = ((res.get("note") + " ") if res.get("note") else "") + \
                "A playlist link asks before anything is queued; a real call looks at it first."
    return res


def installed():
    return importlib.util.find_spec("yt_dlp") is not None
