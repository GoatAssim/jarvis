"""dl_add_http: queue plain file links with the HTTP engine's own options (Download Hub, N4, brief 31).

`dl_add` stays the router and still takes any link; this is the add tool for the one engine whose
choices are not video or torrent choices: `segments`, how many parallel parts THIS file may be fetched
in (the HTTP engine's segmented download, decision 13), and (brief 7C) `filename`, the name ONE file is
saved as, and `referer`, the Referer sent on the first request.  There is NO `headers` argument on
purpose: an Authorization or Cookie value would be stored in the database in clear text.  It forces the HTTP engine, so a
video page or a magnet link is refused with a pointer to the right tool instead of being routed away
quietly.

Everything it does besides that is the shared `_queue` of download_hub.py (the dry run, the bulk
question, the open-the-file question, the full-path question, then `service.add_items`), so it cannot
drift from `dl_add` and the other add tools.  The value is checked once, in `service.add_items`
(`normalize_engine_item_options`); this file only passes it on.

Owner only, like every add tool: a chat guest is refused in the handler, and tools.py also hides every
`dl_add_*` tool from a guest by its name.  No TOOL_PACK_INSTRUCTION here on purpose: the "downloads" group
has ONE, in download_hub.py (tests/test_hub_guest_gates.py checks that only one file declares it).
"""

TOOL_GROUP = "downloads"

# "download this file" / "queue a download" stay with dl_add; these phrases are about splitting one
# file over several connections, which only this tool can do.
TOOL_KEYWORDS = {
    "dl_add_http": {
        "download in parallel parts": 10,
        "download in several parts": 10,
        "split the download": 10,
        "split this download": 10,
        "download with multiple connections": 10,
        "download faster with segments": 10,
        "segmented download": 10,
        "download in segments": 10,
        "parallel download": 9,
        "multi connection download": 9,
        "download and save as": 9,
        "download and rename": 9,
        "download with this file name": 9,
        "download with a referer": 9,
    },
}

TOOL_SCHEMAS = [{
    "name": "dl_add_http",
    "description": (
        "Queue a plain file link (a direct file, an ISO, an installer, a PDF) in the Download Hub with the HTTP "
        "engine, and choose how many parallel parts THIS file is fetched in (segments 2 to 16; 1 = one "
        "connection). Parts only help for a big file from a server that supports Range requests; "
        "otherwise it quietly downloads in one piece. Can also set the saved file name (one link) and a "
        "Referer. Returns at once; the hub daemon does the transfer. "
        "Not for video pages (dl_add_ytdlp) or torrents (dl_add_stremio). dry_run only names the engine."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The file link to download."},
            "urls": {"type": "array", "items": {"type": "string"},
                     "description": "Several file links with the same options (one group)."},
            "segments": {"type": "integer", "minimum": 1, "maximum": 16,
                         "description": ("Parallel parts for this download, 1 to 16 (1 = off for this one). Leave "
                                         "out to follow the person's own setting (http.segments, off by default).")},
            "filename": {"type": "string",
                         "description": ("File name to save THIS download as, e.g. 'report.pdf' (a name, not a path; dest is the "
                                         "folder). One link only. A name the hub would have to change is refused, not fixed.")},
            "referer": {"type": "string",
                        "description": ("An http(s) link sent as the Referer, for a site that needs one. No user name or "
                                        "password in it.")},
            "dest": {"type": "string",
                     "description": ("Where THIS download is saved: a sub-folder of the library root (e.g. 'Manuals') "
                                     "or a full path like 'D:/Games' (the person confirms a full path).")},
            "group": {"type": "string", "description": "A name for this batch."},
            "start_paused": {"type": "boolean", "description": "Queue it paused instead of starting it."},
            "start_at": {"type": "string",
                         "description": ("Hold it until a time, in the person's words: 'tomorrow at 3am', 'in 2 hours'. "
                                         "One time, not a repeat. Wins over start_paused.")},
            "dry_run": {"type": "boolean", "description": "Only say which engine would take each link; queue nothing."},
        },
    },
}]


def _hub():
    """download_hub.py's shared helpers (`_queue`, the guest check).  Imported inside the handler, as
    that file does with the hub package, so loading this tool costs nothing."""
    from . import download_hub
    return download_hub


def _segments(args):
    """The `segments` argument -> (value or None, error).  The range is decided once, in service."""
    raw = args.get("segments")
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, bool):
        return None, "segments must be a whole number from 1 to 16"
    try:
        num = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, "segments must be a whole number from 1 to 16"
    return num, None


def _text_option(args, name):
    """The `filename` / `referer` argument -> (text or None, error).  Missing or an empty string is 'not given';
    anything but text is an error; what the text may hold is decided once, in service (ITEM_OPTION_CHECKS)."""
    raw = args.get(name)
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str):
        return None, "%s must be text" % name
    return raw, None


def tool_dl_add_http(args):
    """Queue plain file links with the HTTP engine and its own options (parallel parts, file name, Referer)."""
    try:
        args = args or {}
        hub = _hub()
        if hub._is_chat_guest():
            return hub._guest_refusal()
        urls = hub._gather_urls(args)
        if not urls:
            return {"needs_clarification": True, "message": "Which file link should I download?"}
        segments, problem = _segments(args)
        if problem:
            return {"ok": False, "error": problem}
        item_options = {"segments": segments} if segments is not None else {}
        for name in ("filename", "referer"):
            text, problem = _text_option(args, name)
            if problem:
                return {"ok": False, "error": problem}
            if text is not None:
                item_options[name] = text
        if "filename" in item_options and len(urls) != 1:
            return {"ok": False, "error": ("filename is for exactly one link; you gave %d. Queue each link on its own "
                                           "with its own filename." % len(urls))}
        item_options = item_options or None
        if item_options:
            # checked here too, so a dry run refuses a bad name or Referer the same way a real add does
            _clean, problem = hub._svc().normalize_engine_item_options(item_options)
            if problem:
                return {"ok": False, "error": problem}
        res = hub._queue(args, urls, "http", None, "dl_add_http", item_options=item_options)
        if isinstance(res, dict) and res.get("dry_run") and item_options:
            res["item_options"] = dict(item_options)
        return res
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return {"ok": False, "error": "dl_add_http failed: %s" % exc}


TOOLS = {"dl_add_http": tool_dl_add_http}

TEST_CHECKLIST = {
    "dl_add_http": {
        "does": "Queues plain file links with the HTTP engine and lets one download choose its own number of parallel parts, its file name and its Referer.",
        "steps": [
            {"run": {"url": "<a direct link to a big file, 100 MB or more, from a server that supports Range>",
                     "segments": 4, "dry_run": True},
             "expect": "ok: true, dry_run: true, one decision with engine 'http', and item_options {segments: 4}; nothing is queued."},
            {"run": {"url": "<the same link>", "segments": 4},
             "expect": "ok: true, one row with engine http; in Menu > Downloads the log says it downloads in parallel parts; "
                       "dl_list shows prefs.segments 4 for it. ⚠️ Needs a real server that supports Range."},
            {"run": {"url": "<the same link>", "segments": 99},
             "expect": "ok: false, the error says segments must be between 1 and 16; nothing is queued."},
            {"run": {"url": "https://www.youtube.com/watch?v=<an id>", "segments": 4},
             "expect": "ok: false, the problem says the HTTP engine can't take that input (a video page goes to dl_add_ytdlp); nothing is queued."},
            {"run": {"url": "<a direct file link>", "segments": 1, "start_paused": True},
             "expect": "ok: true, the row is paused; segments 1 means one connection for this download whatever http.segments says."},
            {"run": {"url": "<a direct file link>", "filename": "my-report.pdf", "dry_run": True},
             "expect": "ok: true, dry_run: true, item_options {filename: 'my-report.pdf'}; nothing is queued."},
            {"run": {"url": "<a direct file link>", "filename": "my-report.pdf", "start_paused": True},
             "expect": "ok: true, one paused row. Resume it: the file lands in the download folder as my-report.pdf (as 'my-report (1).pdf' if that name was taken), not under the server's own name."},
            {"run": {"url": "<a direct file link>", "filename": "../evil.exe", "dry_run": True},
             "expect": "ok: false, the error says a filename can't hold '/' or '..' (a name, not a path); nothing is queued."},
            {"run": {"urls": ["<link one>", "<link two>"], "filename": "a.bin"},
             "expect": "ok: false, the error says filename is for exactly one link; nothing is queued."},
            {"run": {"url": "<a direct file link>", "referer": "https://example.com/the-page", "start_paused": True},
             "expect": "ok: true, one paused row. ⚠️ Needs a real site that checks the Referer to see the effect; tested here only against a fake server."},
            {"run": {"url": "<a direct file link>", "referer": "https://user:secret@example.com/", "dry_run": True},
             "expect": "ok: false, the error says a referer can't hold a user name or password."},
        ],
        "care": "Queues a real download (writes a file in the library root once resumed); use start_paused or dry_run to look without starting.",
        "watch": ["Owner only: from a Discord / Instagram guest it is refused and not even offered. dl_add still takes the same links without segments.",
                  "There is no headers argument: no Authorization or Cookie is ever stored."],
    },
}
