"""Auto-discovered tool file: the Download Hub (JARVIS_DOWNLOAD_HUB.md, phases 1-2).

One Menu > Downloads panel and one background daemon for every download.  This file is
only the thin, model-facing edge: every rule (what may be queued, what pause means in each
state, what is never opened) lives in jarvis/hub/service.py so the panel and the tools can't
drift apart.  See jarvis/hub/__init__.py for the module map and where later phases plug in.

What is in it
-------------
  dl_add / dl_list / dl_status                 queue and look
  dl_pause / dl_resume / dl_cancel / dl_retry  control (hub downloads; provider rows are
                                               read-only until phase 5)
  dl_open                                      open or reveal a FINISHED file, open its folder,
                                               or open the download folder (path from the
                                               database / settings, only inside the library root)
  dl_settings                                  library root, concurrency, rate limit, the
                                               private-host allow-list (confirm-gated)
  dl_prefs                                     the everyday preferences, no confirmation: speed
                                               limit + timed windows, folder names, open-when-
                                               done, which notifications fire (delivered by the
                                               normal notifier), and one download's own options
  hub_doctor                                   is it healthy?
  hub_demo_provider                            fake rows to check the panel by hand
  TOOL_UI                                      the Menu > Downloads panel (hub_ui/)
  DAEMONS                                      the hub daemon (Menu > Daemons)

Never import ai_client / tool_router / tool_registry here at module level (circular import:
the tool would silently vanish); the hub package is import-light but is still only imported
inside the handlers, so a `jarvis` command that never touches downloads pays nothing.
"""

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _svc():
    from ..hub import service     # lazy: see the module docstring
    return service


def _fail(label, exc):
    return {"ok": False, "error": "%s failed: %s" % (label, exc)}


def _selector(args):
    """The id / group / engine / provider / all selector shared by the control tools."""
    return {
        "id": (args.get("id") or "").strip() or None,
        "group": (args.get("group") or "").strip() or None,
        "engine": (args.get("engine") or "").strip() or None,
        "provider": (args.get("provider") or "").strip() or None,
        "all": bool(args.get("all")),
    }


def _confirm_open(count=1):
    """Opening a downloaded FILE by itself is the one 'when done' choice that runs something the
    owner hasn't looked at, so choosing it from here asks first (default=False)."""
    from .. import ui_bridge as ui
    return ui.confirm("Open downloaded files automatically?",
                      body=("When %s finish%s, Jarvis will open %s with its default program. Programs and "
                            "scripts are never opened this way; they are only shown in their folder."
                            % ("these downloads" if count > 1 else "this download", "" if count > 1 else "es",
                               "each file" if count > 1 else "the file")),
                      level="warn", confirm_label="Open them", cancel_label="Don't", default=False)


def _confirm_outside(path, count=1):
    """A full-path folder writes outside the library root, so choosing one from here asks first
    (default=False: an unattended run saves nothing outside the library)."""
    from .. import ui_bridge as ui
    return ui.confirm("Save outside the downloads library?",
                      body=("%s will be saved in:\n%s\n\nThat folder is outside your downloads library." %
                            ("These downloads" if count > 1 else "This download", str(path)[:200])),
                      level="warn", confirm_label="Save there", cancel_label="Don't", default=False)


# ---------------------------------------------------------------------------
# handlers: queue and look
# ---------------------------------------------------------------------------


def tool_dl_add(args):
    try:
        args = args or {}
        urls = []
        if args.get("url"):
            urls.append(str(args["url"]))
        extra = args.get("urls")
        if isinstance(extra, list):
            urls += [str(u) for u in extra]
        elif isinstance(extra, str) and extra.strip():
            urls.append(extra)
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            return {"needs_clarification": True, "message": "Which link should I download?"}
        svc = _svc()
        engine = (args.get("engine") or "").strip() or None

        if args.get("dry_run"):
            decisions = []
            for text in urls[:10]:
                d = svc.preview(text, engine=engine)
                decisions.append({"input": text[:120], "ok": d["ok"], "engine": d["engine"],
                                  "reason": d["reason"], "problem": d["problem"]})
            return {"ok": True, "dry_run": True, "decisions": decisions}

        limits = (svc.get_settings().get("settings") or {}).get("limits") or {}
        if len(urls) > int(limits.get("confirm_above_items", 5)):
            # A bulk add is the one place a question is asked from inside the handler:
            # default=False, so an unattended run queues nothing nobody approved.
            from .. import ui_bridge as ui
            if not ui.confirm("Queue %d downloads?" % len(urls),
                              body="\n".join(u[:100] for u in urls[:8]) + ("\n..." if len(urls) > 8 else ""),
                              level="warn", confirm_label="Queue them", cancel_label="Cancel", default=False):
                return {"ok": False, "cancelled": True, "error": "not confirmed: nothing was queued"}
        open_when_done = args.get("open_when_done")
        if str(open_when_done or "").strip().lower() == "open" and not _confirm_open(len(urls)):
            return {"ok": False, "cancelled": True, "error": "not confirmed: nothing was queued"}
        dest = args.get("dest")
        allow_abs = False
        if dest and svc.settings_mod.is_absolute_spec(dest):
            if not _confirm_outside(dest, len(urls)):
                return {"ok": False, "cancelled": True, "error": "not confirmed: nothing was queued"}
            allow_abs = True
        return svc.add_items(urls, engine=engine, dest=dest, group=args.get("group"),
                             start_paused=bool(args.get("start_paused")),
                             speed_limit_kbps=args.get("speed_limit_kbps"),
                             open_when_done=open_when_done, notify_me=args.get("notify"),
                             notify_channels=args.get("notify_channels"),
                             notify_events=args.get("notify_events"),
                             allow_absolute_dest=allow_abs)
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return _fail("dl_add", exc)


def tool_dl_list(args):
    try:
        args = args or {}
        try:
            limit = max(1, min(500, int(args.get("limit") or 50)))
        except (TypeError, ValueError):
            limit = 50
        return _svc().list_items(
            state=args.get("state"), engine=args.get("engine"), provider=args.get("provider"),
            kind=args.get("kind"), group=args.get("group"), search=args.get("search"),
            limit=limit, with_summary=bool(args.get("summary")))
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_list", exc)


def tool_dl_status(args):
    try:
        return _svc().status(item_id=((args or {}).get("id") or "").strip() or None)
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_status", exc)


# ---------------------------------------------------------------------------
# handlers: control
# ---------------------------------------------------------------------------


def _control(verb, args):
    try:
        return _svc().control(verb, **_selector(args or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_" + verb, exc)


def tool_dl_pause(args):
    return _control("pause", args)


def tool_dl_resume(args):
    return _control("resume", args)


def tool_dl_cancel(args):
    return _control("cancel", args)


def tool_dl_retry(args):
    return _control("retry", args)


def tool_dl_open(args):
    try:
        args = args or {}
        if str(args.get("target") or "").strip().lower() in ("library", "download_folder", "folder_root"):
            return _svc().open_library()
        return _svc().open_item((args.get("id") or "").strip(), action=(args.get("action") or "open").strip())
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_open", exc)


def tool_dl_settings(args):
    try:
        args = args or {}
        changes = args.get("set")
        if changes is None or changes == {}:
            return _svc().get_settings()
        if not isinstance(changes, dict):
            return {"ok": False, "error": "`set` must be an object like {\"concurrency.http\": 2}"}
        return _svc().update_settings(changes)
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_settings", exc)


def tool_dl_prefs(args):
    """Everyday preferences and one download's own options, without the dl_settings question."""
    try:
        args = args or {}
        svc = _svc()
        event = (args.get("test_notification") or "").strip()
        if event:
            return svc.send_test_notification(event)
        changes = args.get("set")
        item_id = (args.get("id") or "").strip() or None
        if changes is None or changes == {}:
            if item_id:
                return {"ok": False, "error": "say what to change for that download: {\"limit_kbps\": 500}"}
            return svc.get_settings()
        if not isinstance(changes, dict):
            return {"ok": False, "error": "`set` must be an object like {\"rate_limit_kbps\": 2048}"}
        wants_open = (changes.get("after_download.open") if not item_id
                      else (changes.get("open") or changes.get("open_when_done")))
        if str(wants_open or "").strip().lower() == "open" and not _confirm_open():
            return {"ok": False, "cancelled": True, "error": "not confirmed: nothing was changed"}
        allow_abs = False
        if item_id:
            folder = changes.get("folder") if "folder" in changes else changes.get("dest")
            if folder and svc.settings_mod.is_absolute_spec(folder):
                if not _confirm_outside(folder):
                    return {"ok": False, "cancelled": True, "error": "not confirmed: nothing was changed"}
                allow_abs = True
        return svc.update_prefs(changes, id=item_id, allow_absolute_dest=allow_abs)
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_prefs", exc)


def tool_hub_doctor(args):
    try:
        from ..hub import doctor
        return doctor.run()
    except Exception as exc:  # noqa: BLE001
        return _fail("hub_doctor", exc)


def tool_hub_demo_provider(args):
    try:
        from ..hub import demo
        from ..hub.store import Store
        action = ((args or {}).get("action") or "status").strip().lower()
        store = Store()
        if action == "start":
            out = demo.start(store)
            if out.get("ok") and not _svc().daemon_view(store)["alive"]:
                out["note"] = ("The hub daemon isn't running, so the demo rows won't move (they will go "
                               "stale, which is also a thing to look at). Start it in Menu > Daemons.")
            return out
        if action == "stop":
            return demo.stop(store)
        if action == "clear":
            return demo.clear(store)
        if action == "status":
            return demo.status(store)
        return {"ok": False, "error": "action must be start, stop, clear or status"}
    except Exception as exc:  # noqa: BLE001
        return _fail("hub_demo_provider", exc)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

_SELECTOR_PROPS = {
    "id": {"type": "string", "description": "One download's id, from dl_list."},
    "group": {"type": "string", "description": "A group id (a batch or season) from dl_list: applies to every download in it."},
    "engine": {"type": "string", "description": "Apply to every download of this engine (e.g. http)."},
    "provider": {"type": "string", "description": "Apply to every row of this provider."},
    "all": {"type": "boolean", "description": "Apply to everything that can take it."},
}

TOOL_SCHEMAS = [
    {
        "name": "dl_add",
        "description": (
            "Queue a download in the Download Hub and return at once; the hub's background daemon does "
            "the transfer and shows it in Menu > Downloads. Use for a direct file link (http/https). "
            "Not for YouTube pages (use ytdl_download) and magnet links are not supported yet. "
            "Set dry_run to see which engine would take a link without queueing it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The link to download."},
                "urls": {"type": "array", "items": {"type": "string"}, "description": "Several links at once (queued as one group)."},
                "engine": {"type": "string", "description": "Force an engine (e.g. http) instead of letting the router choose."},
                "dest": {"type": "string", "description": "Where THIS download is saved, overriding the per-extension and per-type folders: a sub-folder of the library root (e.g. 'Manuals') or a full path like 'D:/Games' (the person is asked to confirm a full path)."},
                "group": {"type": "string", "description": "A name for this batch."},
                "start_paused": {"type": "boolean", "description": "Queue it paused instead of starting it."},
                "speed_limit_kbps": {"type": "integer", "description": "A speed cap for just this download, in KB/s (0 or missing = only the global limit applies)."},
                "open_when_done": {"type": "string", "enum": ["default", "off", "reveal", "folder", "open"], "description": "What to do when it finishes: nothing, show the file in its folder, open the folder, or open the file (asks the person first; programs and scripts are never opened). Default = the person's own setting."},
                "notify": {"type": "string", "enum": ["default", "on", "off"], "description": "Tell the person when this one finishes or fails (on), never (off), or follow their notification settings (default)."},
                "notify_channels": {"type": "array", "items": {"type": "string", "enum": ["inbox", "stream", "desktop", "voice", "playnite", "discord", "instagram"]}, "description": "Where to announce THIS download, instead of the person's usual channels, e.g. [\"discord\"] for Discord only (the notification inbox keeps its record either way). Omit to follow the person's own settings."},
                "notify_events": {"type": "array", "items": {"type": "string", "enum": ["started", "done", "failed", "stalled"]}, "description": "Which events announce THIS download, e.g. [\"done\", \"failed\"]. Omit to follow the person's own settings. A download with its own choices is announced even if notifications are off globally."},
                "dry_run": {"type": "boolean", "description": "Only say which engine would take each link; queue nothing."},
            },
        },
    },
    {
        "name": "dl_list",
        "description": (
            "List downloads (what is downloading, queued, failed, finished) with progress, speed, ETA "
            "and destination. Use for 'what's downloading?', 'did the download finish?', 'how far along is it?'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "A state (queued, downloading, paused, stalled, failed, cancelled, done ...) or active / final / waiting; comma-separate several."},
                "engine": {"type": "string", "description": "Only this engine."},
                "provider": {"type": "string", "description": "Only this tool-supplied provider."},
                "kind": {"type": "string", "enum": ["video", "audio", "file", "torrent", "game", "other"]},
                "group": {"type": "string", "description": "Only this group id."},
                "search": {"type": "string", "description": "Text to find in titles."},
                "limit": {"type": "integer", "description": "How many rows (default 50, at most 500)."},
                "summary": {"type": "boolean", "description": "Also include the totals (counts, speed, daemon, free disk)."},
            },
        },
    },
    {
        "name": "dl_status",
        "description": (
            "Totals for the Download Hub: how many are active / queued / failed / stale, hub vs other "
            "speed, whether the hub daemon is running, the library folder and free disk, which engines are ready. "
            "Pass an id for one download in full (its recent events, files, details)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "One download's id (from dl_list) for its full detail."}},
        },
    },
    {
        "name": "dl_pause",
        "description": "Pause a download, a group, an engine's downloads, or everything (all=true). Keeps the partial file so it can resume.",
        "parameters": {"type": "object", "properties": dict(_SELECTOR_PROPS)},
    },
    {
        "name": "dl_resume",
        "description": "Resume paused downloads (one id, a group, an engine, or all=true).",
        "parameters": {"type": "object", "properties": dict(_SELECTOR_PROPS)},
    },
    {
        "name": "dl_cancel",
        "description": "Cancel downloads and delete their partial files (one id, a group, an engine, or all=true). The person is asked to confirm. Finished files are never deleted.",
        "parameters": {"type": "object", "properties": dict(_SELECTOR_PROPS)},
    },
    {
        "name": "dl_retry",
        "description": "Retry failed, stalled or cancelled downloads (one id, a group, an engine, or all=true).",
        "parameters": {"type": "object", "properties": dict(_SELECTOR_PROPS)},
    },
    {
        "name": "dl_open",
        "description": "Open a finished download with its default program, reveal it in the file manager, or open its folder; or open the whole download folder (target=library). Only inside the library folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The download's id, from dl_list."},
                "action": {"type": "string", "enum": ["open", "reveal", "folder"], "description": "open (default), reveal in the folder, or open the folder it is in."},
                "target": {"type": "string", "enum": ["library"], "description": "Open the download folder itself (no id needed)."},
            },
        },
    },
    {
        "name": "dl_settings",
        "description": (
            "Read or change the Download Hub settings: library_root (where files go), concurrency.total / "
            "concurrency.http, rate_limit_kbps (0 = unlimited), http.stall_seconds, http.max_retries, "
            "allow_private_hosts (hosts the hub may fetch even though they are on the local network), and "
            "ext_folders when it uses a full path ({\".iso\": \"D:/ISOs\"}). "
            "Call with no `set` to read. The person is asked to confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "set": {"type": "object", "description": "Settings to change, e.g. {\"concurrency.http\": 2, \"library_root\": \"D:/Downloads/Jarvis\"}."},
            },
        },
    },
    {
        "name": "dl_prefs",
        "description": (
            "Read or change the everyday Download Hub preferences without a confirmation: the speed limit "
            "(rate_limit_kbps, 0 = unlimited) and timed speed windows (schedule), the folder name for each "
            "kind of file (folders.video / folders.audio / folders.file), a folder for each file extension "
            "(ext_folders, the whole map, e.g. {\".iso\": \"ISOs\", \".pdf\": \"Docs\"}: a sub-folder of the library; "
            "an empty value removes one; it wins over the per-kind folder), what happens when a download finishes "
            "(after_download.open: off, reveal, folder, open), which notifications are on (notify.done.enabled, "
            "notify.failed.enabled, notify.started, notify.stalled, notify.group_done, notify.queue_empty, each "
            "with an optional .level 1-5 for the notifier), and connections (concurrency.*, http.*). With `id`, "
            "changes one download's own limit_kbps / open / notify, where it is announced (notify_channels, e.g. "
            "[\"discord\"]), which events announce it (notify_events) and the folder it saves to (folder, until it "
            "has started; a full path asks the person first). test_notification=<type> sends a sample "
            "notification. Where files are saved (library_root) and allow_private_hosts need dl_settings. "
            "Call with no arguments to read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "set": {"type": "object", "description": "Preferences to change, e.g. {\"rate_limit_kbps\": 2048, \"notify.done.enabled\": true, \"after_download.open\": \"reveal\"}; with id: {\"limit_kbps\": 500}."},
                "id": {"type": "string", "description": "One download's id: change just its own speed limit / open-when-done / notify / notify_channels / notify_events / folder."},
                "test_notification": {"type": "string", "enum": ["started", "done", "failed", "stalled", "group_done", "queue_empty"], "description": "Send a sample of this notification type."},
            },
        },
    },
    {
        "name": "hub_doctor",
        "description": "Check the Download Hub is healthy: the store, the daemon, the library folder and free disk, each engine and provider. Use when downloads don't start or the panel looks wrong.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "hub_demo_provider",
        "description": "Start, stop or clear the fake demo downloads that show every look of the Downloads panel (a climbing bar, indeterminate, stalled, failed, a silent provider going stale). For checking the panel by hand; the person is asked to confirm.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "clear", "status"]},
            },
            "required": ["action"],
        },
    },
]

TOOLS = {
    "dl_add": tool_dl_add,
    "dl_list": tool_dl_list,
    "dl_status": tool_dl_status,
    "dl_pause": tool_dl_pause,
    "dl_resume": tool_dl_resume,
    "dl_cancel": tool_dl_cancel,
    "dl_retry": tool_dl_retry,
    "dl_open": tool_dl_open,
    "dl_settings": tool_dl_settings,
    "dl_prefs": tool_dl_prefs,
    "hub_doctor": tool_hub_doctor,
    "hub_demo_provider": tool_hub_demo_provider,
}

# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

TOOL_GROUP = "downloads"

# ytdl_download keeps "download video" / "download youtube" / "download this video" (weight 10);
# these generic phrases must not take those over, hence the not_with lists and needs_any.
_NOT_MEDIA = ["video", "youtube", "song", "track", "episode", "movie", "folder", "directory"]
TOOL_KEYWORDS = {
    "dl_add": {
        "download this file": {"weight": 10, "not_with": _NOT_MEDIA},
        "download the file": {"weight": 10, "not_with": _NOT_MEDIA},
        "queue a download": 10,
        "add a download": 10,
        "download this link": {"weight": 9, "not_with": _NOT_MEDIA},
        "magnet link": 8,
    },
    "dl_list": {
        "download queue": 10,
        "what's downloading": 10,
        "whats downloading": 10,
        "what is downloading": 10,
        "active downloads": 10,
        "my downloads": {"weight": 8, "not_with": ["folder", "directory"]},
        "downloads": {"weight": 6, "not_with": _NOT_MEDIA},
        "how far along": {"weight": 6, "needs_any": ["download", "downloads", "downloading"]},
    },
    "dl_status": {"download status": 10, "download hub": 10, "downloads menu": 9},
    "dl_pause": {"pause downloads": 10, "pause the download": 10, "pause all downloads": 10},
    "dl_resume": {"resume downloads": 10, "resume the download": 10},
    "dl_cancel": {"cancel the download": 10, "cancel downloads": 10},
    "dl_retry": {"retry the download": 10, "retry downloads": 10, "retry failed downloads": 10},
    "dl_open": {"open the download": 9, "reveal the download": 9, "open the download folder": 10},
    "dl_settings": {"download settings": 9, "download folder": {"weight": 8, "not_with": ["open"]}},
    "dl_prefs": {"download speed limit": 10, "limit download speed": 10, "limit the download speed": 10,
                 "download notifications": 10, "notify me when downloads": 10,
                 "notify when the download": 10, "open when done": 9, "open when the download": 9},
    "hub_doctor": {"hub doctor": 10, "downloads aren't starting": 10, "downloads not starting": 10},
    "hub_demo_provider": {"demo downloads": 10, "downloads demo": 10},
}

TOOL_PACK_INSTRUCTION = (
    "dl_add queues and returns at once; the hub daemon does the transfer, so report progress "
    "with dl_list / dl_status instead of waiting. YouTube pages -> ytdl_download; magnet links "
    "are not supported by dl_add yet."
)

# ---------------------------------------------------------------------------
# confirm gating (out-of-band, by tool name)
# ---------------------------------------------------------------------------

# cancel deletes partial files; settings can move where files go and open the private-network
# allow-list; the demo fills the owner's list with fake rows. dl_add's bulk question is asked
# inside the handler (default=False).  dl_prefs is NOT listed: it only holds the everyday keys
# (speed, notifications, folder names inside the library), and the one choice that runs
# something unseen (open-the-file when done) asks inside the handler, default=False.
TOOL_CONFIRM_REQUIRED = {"dl_cancel", "dl_settings", "hub_demo_provider"}
TOOL_AI_REVIEW = set()

TOOL_RESULT_SPECS = {
    "dl_list": {
        "drop_fields": {"medium": [], "low": ["summary"]},
        "list_item_drop": {"items": {
            "medium": ["links", "provider_icon", "pending", "phase_index", "phase_count", "derived",
                       "created_at", "finished_at", "age_s"],
            "low": ["links", "provider_icon", "pending", "phase_index", "phase_count", "derived",
                    "created_at", "finished_at", "age_s", "source", "subtitle", "group_id", "peers",
                    "seeds", "capabilities", "retry_in_s"]}},
        "list_item_truncate": {"items": {"title": {"medium": 120, "low": 60}, "error": {"medium": 160, "low": 80}}},
    },
    "dl_status": {
        "drop_fields": {"medium": ["engine_load_errors"], "low": ["engine_load_errors", "settings", "engines"]},
    },
}

# ---------------------------------------------------------------------------
# the panel and the daemon
# ---------------------------------------------------------------------------

TOOL_UI = [
    {
        "id": "downloads_panel",
        "label": "Downloads",
        "mode": "menu",
        "size": "wide",
        "path": "hub_ui",
        "title": "Downloads",
        "hint": "Everything downloading, queued, failed or finished",
        "tool": "dl_list",
    },
]

# The runner script adds the package's folder to sys.path itself, so it works the same from a
# source checkout and from an installed copy. It deliberately declares no `cwd`: a shipped daemon
# defaults to the config folder (tool_daemons._default_cwd), because a process running with its
# cwd inside the installed package blocks the rebuild's `pip install --force-reinstall`.
DAEMONS = [
    {
        "id": "hub",
        "name": "Download hub",
        "description": "Runs the Download Hub's transfers: queue, retries, resume, and the demo provider.",
        "command": ["{python}", "{tool_dir}/_hub_daemon.py"],
        "restart": "always",
        "autostart": True,
        "categories": ["Downloads"],
    },
]

# ---------------------------------------------------------------------------
# Test Checklist (Menu > Test Checklist)
# ---------------------------------------------------------------------------

TEST_CHECKLIST_GROUP = {
    "label": "Downloads (hub)",
    "blurb": "The Download Hub: queue, progress, pause / resume / cancel, the Menu > Downloads panel and its demo provider.",
}

_LOCAL = "http://127.0.0.1:<port>/<file>"

TEST_CHECKLIST = {
    "dl_add": {
        "does": "Queues a direct http(s) link in the Download Hub; the daemon downloads it in the background.",
        "steps": [
            {"run": {"url": "https://<a real file link>", "dry_run": True},
             "expect": "ok: true, one decision: engine http, reason 'direct link', nothing queued."},
            {"run": {"url": "https://<a real, small file link>"},
             "expect": "ok: true, one item added with an id; it appears in Menu > Downloads and finishes (daemon running)."},
            {"run": {"url": "http://192.168.1.1/"},
             "expect": "ok: false, problem says it points at a private or local address (refused by the SSRF guard)."},
            {"run": {"url": "https://<a real file link>", "dest": "../../etc"},
             "expect": "ok: false: dest must be a sub-folder of the library root."},
            {"run": {"url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"},
             "expect": "ok: false, says magnet links need the stremio engine which isn't installed yet."},
            {"ask": "Download this file: https://<a real, small file link>",
             "expect": "Calls dl_add (not ytdl_download) and says it was queued."},
        ],
        "needs": ["The 'Download hub' daemon running (Menu > Daemons)", "Internet access for the real-link steps"],
        "care": "Writes the file under the library root (default ~/Downloads/Jarvis/Files). A local test server needs allow_private_hosts set first.",
        "watch": ["Adding the same link twice returns the existing item (duplicate: true), not a second download.",
                  "More than 5 links in one call asks for confirmation first.",
                  "Kill the daemon mid-file (Menu > Daemons > Stop), start it again: the row goes back to queued and the file resumes, not restarts (needs a server that supports Range)."],
    },
    "dl_list": {
        "does": "Lists downloads with state, percent, speed, ETA, size, destination; filter by state, engine, provider, kind, group or text.",
        "steps": [
            {"run": {"summary": True},
             "expect": "ok: true, items (newest first) and a summary with counts, daemon alive and the library root."},
            {"run": {"state": "active"},
             "expect": "Only rows that are probing, connecting, downloading or post-processing."},
            {"run": {"state": "bogus"},
             "expect": "ok: false naming the valid states."},
            {"ask": "What's downloading right now?",
             "expect": "Calls dl_list and reports each active download with its percent."},
        ],
        "watch": ["Rows of a provider switched off in the panel are hidden."],
    },
    "dl_status": {
        "does": "Totals and health: active / queued / failed / stale, hub vs other speed, daemon state, library root, free disk, engines.",
        "steps": [
            {"run": {}, "expect": "ok: true with counts, daemon {alive, pid}, library_root, free_bytes, engines (http ready)."},
            {"run": {"id": "<a download id>"}, "expect": "ok: true with that download's item including events (recent state changes), detail and children."},
            {"ask": "Is the download hub running?", "expect": "Says whether the daemon is alive."},
        ],
    },
    "dl_pause": {
        "does": "Pauses one download, a group, an engine's downloads or everything, keeping the partial file.",
        "steps": [
            {"run": {"id": "<a downloading id>"},
             "expect": "ok: true, the id is in 'pending' (the daemon applies it within about a second) then the row shows paused."},
            {"run": {"id": "<a done id>"},
             "expect": "ok: false, 'already done'."},
            {"run": {},
             "expect": "ok: false asking which download (an id, a group, an engine, a provider, or all=true)."},
        ],
        "watch": ["Pausing a row from a read-only provider (the demo rows) says it is read-only, never fails silently."],
    },
    "dl_resume": {
        "does": "Resumes paused downloads from where they stopped.",
        "steps": [
            {"run": {"id": "<a paused id>"}, "expect": "ok: true, the row goes to queued and then downloads again from its byte offset."},
            {"run": {"all": True}, "expect": "ok: true; rows that weren't paused are listed under skipped, not an error."},
        ],
    },
    "dl_cancel": {
        "does": "Cancels downloads and deletes their .part files; finished files are never touched.",
        "steps": [
            {"run": {"id": "<a downloading or paused id>"},
             "expect": "Asks to confirm, then ok: true; the row is cancelled and its .part file is gone."},
            {"run": {"id": "<a done id>"}, "expect": "ok: false, 'already done'; the finished file is still there."},
        ],
        "care": "Deletes the partial file of what you cancel.",
    },
    "dl_retry": {
        "does": "Puts failed, stalled or cancelled downloads back in the queue with a fresh retry budget.",
        "steps": [
            {"run": {"id": "<a failed id>"}, "expect": "ok: true, the row goes to queued and starts again."},
            {"run": {"id": "<a downloading id>"}, "expect": "ok: false, 'nothing to retry'."},
        ],
    },
    "dl_open": {
        "does": "Opens a finished download, or reveals it in the file manager; only inside the library folder.",
        "steps": [
            {"run": {"id": "<a done id>", "action": "reveal"}, "expect": "ok: true; the folder opens with the file selected."},
            {"run": {"id": "<a done id>"}, "expect": "ok: true; the file opens in its default program."},
            {"run": {"id": "<a queued id>"}, "expect": "ok: false, that download has no file yet."},
        ],
        "care": "Launches a program on this PC.",
        "watch": ["The path is never taken from the call: only from the database, and only inside the library root."],
    },
    "dl_settings": {
        "does": "Reads or changes the hub's settings (library root, concurrency, rate limit, stall time, retries, the private-host allow-list).",
        "steps": [
            {"run": {}, "expect": "Asks to confirm, then shows library_root, concurrency, rate_limit_kbps, allow_private_hosts, http."},
            {"run": {"set": {"concurrency.http": 2}}, "expect": "Asks to confirm, then ok: true with concurrency.http 2."},
            {"run": {"set": {"nonsense": 1}}, "expect": "ok: false: that setting can't be changed here (typos are never silently ignored)."},
            {"run": {"set": {"library_root": "relative/path"}}, "expect": "ok: false: must be an absolute path."},
        ],
        "care": "library_root decides where new files are written; allow_private_hosts lets the hub fetch from your local network.",
    },
    "dl_prefs": {
        "does": "Everyday preferences without a confirmation: speed limit and timed windows, folder names per file type, what happens when a download finishes, which notifications fire (they are delivered by the normal notifier), and one download's own speed / open / notify choice.",
        "steps": [
            {"run": {}, "expect": "ok: true with the settings, including notify (six events), after_download, folders, schedule and rate_limit_now."},
            {"run": {"set": {"rate_limit_kbps": 512}}, "expect": "ok: true, rate_limit_now.kbps 512 (source manual); a running download slows to about 512 KB/s within a couple of seconds. Set 0 to lift it."},
            {"run": {"set": {"schedule": [{"days": "all", "from": "00:00", "to": "23:59", "rate_limit_kbps": 256}]}}, "expect": "ok: true, rate_limit_now.kbps 256 with source schedule. Clear it again with {\"schedule\": []}."},
            {"run": {"test_notification": "done"}, "expect": "ok: true; a sample notification appears the way any Jarvis notification does (inbox, toast, desktop per your notification settings) and the note lists the channels that delivered."},
            {"run": {"set": {"notify.done.enabled": False}}, "expect": "ok: true; the next finished download makes no notification. Turn it back on afterwards."},
            {"run": {"set": {"after_download.open": "reveal"}}, "expect": "ok: true; the next finished download is shown in its folder by itself."},
            {"run": {"set": {"after_download.open": "open"}}, "expect": "Asks first (Open them / Don't). Don't: ok: false, cancelled, nothing changed."},
            {"run": {"set": {"library_root": "D:/x"}}, "expect": "ok: false: it needs dl_settings (which asks you first)."},
            {"run": {"id": "<a queued or downloading id>", "set": {"limit_kbps": 300}}, "expect": "ok: true with options.limit_kbps 300; that download is capped at about 300 KB/s while others are not."},
            {"ask": "Limit my downloads to 1 MB/s", "expect": "Calls dl_prefs with rate_limit_kbps 1024 and confirms the new limit."},
        ],
        "care": "Opening finished files automatically is off by default. Programs and scripts (.exe, .bat, .ps1, .js, .lnk, ...) are only ever shown in their folder, never opened.",
        "watch": ["A burst of finishes inside the 'group within N seconds' window arrives as one notification ('3 downloads finished').",
                  "A batch of many files never opens more than a few windows at once."],
    },
    "hub_doctor": {
        "does": "Checks the store, the daemon, the library folder and disk, each engine and provider, and any spooled reports.",
        "steps": [
            {"run": {}, "expect": "ok: true (or a clear problem) with a list of checks; the daemon row says running when the hub daemon is up."},
            {"ask": "Why aren't my downloads starting?", "expect": "Calls hub_doctor and names the cause (for example, the daemon not running)."},
        ],
    },
    "hub_demo_provider": {
        "does": "Starts, stops or clears seven fake downloads that exercise every look of the Downloads panel.",
        "steps": [
            {"run": {"action": "start"}, "expect": "Asks to confirm, then ok: true, 7 rows appear in Menu > Downloads under 'Demo provider'."},
            {"run": {"action": "status"}, "expect": "running: true with elapsed seconds."},
            {"run": {"action": "stop"}, "expect": "Unfinished demo rows become cancelled."},
            {"run": {"action": "clear"}, "expect": "All demo rows are deleted."},
        ],
        "needs": ["The Download hub daemon running (it advances the demo rows)"],
        "watch": ["After about 40 s the 'silent' row goes grey with 'last update ... ago' (stale), not red.",
                  "Every demo row shows the read-only badge and no pause / cancel buttons."],
    },
}
