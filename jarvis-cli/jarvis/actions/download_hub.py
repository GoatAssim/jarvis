"""Auto-discovered tool file: the Download Hub (JARVIS_DOWNLOAD_HUB.md, phases 1-2).

One Menu > Downloads panel and one background daemon for every download.  This file is
only the thin, model-facing edge: every rule (what may be queued, what pause means in each
state, what is never opened) lives in jarvis/hub/service.py so the panel and the tools can't
drift apart.  See jarvis/hub/__init__.py for the module map and where later phases plug in.

What is in it
-------------
  dl_add / dl_list / dl_status                 queue and look (dl_add is the router: it takes any link
                                               and says which engine took it and which add tool carries
                                               that engine's own options)
  dl_add_ytdlp / dl_add_stremio                the per-source add tools (N4): video / audio options,
                                               a torrent's file choice.  A tool file's own source gets a
                                               GENERATED dl_add_<id> (tool_download_add.py), never one
                                               written here
  dl_import_legacy                             index the old ytdl_download folders (read-only)
  dl_providers / dl_provider_prefs             the watched sources (Steam, Playnite, tools with
                                               their own downloads): health, on / off, settings
  dl_pause / dl_resume / dl_cancel / dl_retry  control (hub downloads; a provider row only if
                                               its provider declares the verb, else read-only)
  dl_dismiss                                   hide a provider row that is unknown or finished
                                               (one id, or finished=true); owner only, deletes nothing
  dl_schedule                                  start a download later ("tomorrow 3am"), or clear
                                               that; dl_add takes the same `start_at`
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

import json
import os
import sys

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _notify_types_help():
    """The notification types a download may be announced through, as one sentence built from
    notify_registry (plain data, safe at module level) so a new type reaches the model with no
    edit here. Built when the tool list is read."""
    from .. import notify_registry
    hints = [a["model_hint"] for a in notify_registry.actions(notify_registry.CHANNEL) if a.get("model_hint")]
    return "Any notification type: %s." % ", ".join(hints)


def _svc():
    from ..hub import service     # lazy: see the module docstring
    return service


# The yt-dlp options.  They live in dl_add_ytdlp's schema only (N4); dl_add still ACCEPTS them
# (forwarded to the yt-dlp engine, ignored for a plain file link, marked deprecated in its answer)
# so old callers keep working.
_VIDEO_ARGS = ("mode", "quality", "container", "video_codec", "audio_format", "audio_quality",
               "embed_thumbnail", "embed_metadata", "embed_chapters", "write_subs", "embed_subs",
               "sub_langs", "sponsorblock_remove", "skip_if_downloaded")

# The per-download notification / after-download / speed arguments.  Every add tool still ACCEPTS
# them (the panel's add form and ytdl_download send them), but they are only ADVERTISED in the
# schemas when this is True.  False = the "optional second step" of N4 as far as the schemas go:
# they can be set on a queued row with dl_prefs and its `id`.  Owner decision N4 (OWNER_TODO).
ADVERTISE_PER_DOWNLOAD_EXTRAS = False
_EXTRA_ARGS = ("speed_limit_kbps", "open_when_done", "notify", "notify_channels", "notify_events")

# Which add tool carries an engine's own options.  An engine not listed (http, qbittorrent) has
# none: plain links stay with dl_add.
_ADD_TOOL_FOR_ENGINE = {"ytdlp": "dl_add_ytdlp", "stremio": "dl_add_stremio"}


def _add_tool_for(engine):
    return _ADD_TOOL_FOR_ENGINE.get(engine or "", "dl_add")


def _is_chat_guest():
    """True when this turn answers a chat message from someone who is NOT the owner.  No chat
    context (the PC, a scheduled job) is the owner's own.  Same variable tools.py reads, but an
    unreadable value counts as a guest here: a permission check fails closed."""
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def _guest_refusal():
    return {"ok": False, "error": "adding downloads is the owner's: someone in a chat can look at the "
                                  "queue (dl_list, dl_status) but not add to it"}


def _dismiss_guest_refusal():
    return {"ok": False, "error": "clearing the download list is the owner's: someone in a chat can look at "
                                  "the queue (dl_list, dl_status) but not hide rows from it"}


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


def _gather_urls(args):
    urls = []
    if args.get("url"):
        urls.append(str(args["url"]))
    extra = args.get("urls")
    if isinstance(extra, list):
        urls += [str(u) for u in extra]
    elif isinstance(extra, str) and extra.strip():
        urls.append(extra)
    return [u.strip() for u in urls if u and u.strip()]


def _queue(args, urls, engine, engine_options, label):
    """What every add tool does once it knows its links, the engine and the engine's options: the
    dry run, the bulk question, the open-the-file question, the full-path question, then the
    hub's add_items.  ONE copy, so the three tools cannot drift apart."""
    svc = _svc()
    if args.get("dry_run"):
        decisions = []
        for text in urls[:10]:
            d = svc.preview(text, engine=engine)
            decisions.append({"input": text[:120], "ok": d["ok"], "engine": d["engine"],
                              "reason": d["reason"], "problem": d["problem"],
                              "add_tool": _add_tool_for(d["engine"]) if d["engine"] else None})
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
    res = svc.add_items(urls, engine=engine, dest=dest, group=args.get("group"),
                        start_paused=bool(args.get("start_paused")),
                        speed_limit_kbps=args.get("speed_limit_kbps"),
                        open_when_done=open_when_done, notify_me=args.get("notify"),
                        notify_channels=args.get("notify_channels"),
                        notify_events=args.get("notify_events"),
                        allow_absolute_dest=allow_abs, start_at=args.get("start_at"),
                        engine_options=engine_options)
    if isinstance(res, dict):
        for row in res.get("added") or []:
            if isinstance(row, dict):
                row["add_tool"] = _add_tool_for(row.get("engine"))
    return res


def tool_dl_add(args):
    """The router: any link, the engine is picked as plan section 8 says, and the answer names the
    add tool that carries that engine's own options."""
    try:
        args = args or {}
        if _is_chat_guest():
            return _guest_refusal()
        urls = _gather_urls(args)
        if not urls:
            return {"needs_clarification": True, "message": "Which link should I download?"}
        engine = (args.get("engine") or "").strip() or None
        video = {k: args.get(k) for k in _VIDEO_ARGS if args.get(k) is not None}
        res = _queue(args, urls, engine, video, "dl_add")
        if video and isinstance(res, dict) and not res.get("dry_run"):
            note = ("%s on dl_add is deprecated: use dl_add_ytdlp for video options "
                    "(they were still applied to the video links)." % ", ".join(sorted(video)))
            res["note"] = (res["note"] + " " + note) if res.get("note") else note
        return res
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return _fail("dl_add", exc)


def tool_dl_add_ytdlp(args):
    """Queue a video / audio page with the yt-dlp engine and its own options."""
    try:
        args = args or {}
        if _is_chat_guest():
            return _guest_refusal()
        urls = _gather_urls(args)
        if not urls:
            return {"needs_clarification": True, "message": "Which video link should I download?"}
        video = {k: args.get(k) for k in _VIDEO_ARGS if args.get(k) is not None}
        return _queue(args, urls, "ytdlp", video, "dl_add_ytdlp")
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_add_ytdlp", exc)


def _stremio_source(args):
    """The magnet / infohash / stream object / .torrent link plus the file choice -> (the canonical
    source text, an error).  The file choice is the magnet's `so=` or the stream's `fileIdx`;
    `file_index` sets it and wins over one already in the text."""
    from ..hub.stremio import links
    magnet = args.get("magnet")
    magnet = magnet.strip() if isinstance(magnet, str) else magnet
    stream = args.get("stream")
    if magnet and stream:
        return None, "give a magnet or a stream, not both"
    idx = args.get("file_index")
    if idx is not None and idx != "":
        if isinstance(idx, bool):
            return None, "file_index must be a whole number from 0"
        try:
            idx = int(str(idx).strip())
        except (TypeError, ValueError):
            return None, "file_index must be a whole number from 0"
        if not 0 <= idx <= links.MAX_FILE_INDEX:
            return None, "file_index must be a whole number from 0 to %d" % links.MAX_FILE_INDEX
    else:
        idx = None
    if stream:
        parsed = links.parse_stream(stream)
        if not parsed:
            return None, ("that stream has no infoHash, so it is not a torrent: a direct `url` stream goes "
                          "to dl_add, a YouTube one to dl_add_ytdlp")
    elif magnet:
        parsed = links.parse_input(str(magnet))
        if not parsed or parsed.get("kind") == "server_url":
            return None, "that is not a magnet link, a 40-character infohash, a Stremio stream or a .torrent link"
    else:
        return None, None
    if parsed.get("kind") == "torrent_url":
        if idx is not None:
            return None, ("a .torrent link can't carry a file choice (its file list is only known once "
                          "it is fetched); leave file_index out")
        return parsed["url"], None
    if idx is not None:
        parsed = dict(parsed, file_idx=idx)
    return links.canonical_source(parsed), None


def tool_dl_add_stremio(args):
    """Queue a torrent (magnet, infohash, Stremio stream or .torrent link) with the Stremio engine."""
    try:
        args = args or {}
        if _is_chat_guest():
            return _guest_refusal()
        source, problem = _stremio_source(args)
        if problem:
            return {"ok": False, "error": problem}
        if not source:
            return {"needs_clarification": True, "message": "Which magnet link, infohash or stream should I download?"}
        return _queue(args, [source], "stremio", None, "dl_add_stremio")
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_add_stremio", exc)


def tool_dl_import_legacy(args):
    """Index the old ytdl_download folders as read-only history rows (nothing is moved or deleted)."""
    try:
        from ..hub import legacy
        from ..hub.store import Store
        return legacy.import_legacy(Store())
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_import_legacy", exc)


def tool_dl_providers(args):
    """Every provider (tools with their own downloads, Steam, Playnite) with health and counts."""
    try:
        res = _svc().list_providers()
        # Each provider's add tool, when its file declared an `add` block (generated by the loader).
        # Only asked of a tools module that is already loaded: this file never imports it itself.
        live = getattr(sys.modules.get("jarvis.tools"), "TOOLS", None) or {}
        if isinstance(res, dict):
            for row in res.get("providers") or []:
                if isinstance(row, dict):
                    name = "dl_add_%s" % row.get("id")
                    row["add_tool"] = name if name in live else None
        return res
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_providers", exc)


def tool_dl_provider_prefs(args):
    """The owner's choices for one provider: on / off, poll interval, its settings."""
    try:
        args = args or {}
        return _svc().update_provider((args.get("id") or "").strip(), enabled=args.get("enabled"),
                                      interval_s=args.get("interval_s"), settings=args.get("settings"))
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_provider_prefs", exc)


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


def tool_dl_dismiss(args):
    """Hide a provider row that is unknown or finished (`id`), or every such row (`finished: true`).
    Owner only.  Not confirm-gated: nothing is deleted and no file is touched, and the row comes back
    on its own if its provider reports it live again."""
    try:
        if _is_chat_guest():
            return _dismiss_guest_refusal()
        args = args or {}
        return _svc().dismiss(id=args.get("id"), finished=args.get("finished") is True)
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_dismiss", exc)


def tool_dl_schedule(args):
    """Start downloads later: `when` in plain words, the same grammar as scheduling a daemon."""
    try:
        args = args or {}
        when = args.get("when")
        if when is None:
            return {"needs_clarification": True,
                    "message": "When should it start? (for example 'tomorrow at 3am' or 'in 2 hours'; "
                               "'now' starts it at once, 'clear' removes the start time)"}
        return _svc().schedule_items(str(when), **_selector(args))
    except Exception as exc:  # noqa: BLE001
        return _fail("dl_schedule", exc)


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

# Every property any add tool can carry, written once.  Each tool picks its own (see _add_props).
_ALL_PROPS = {
    "url": {"type": "string", "description": "The link to download."},
    "urls": {"type": "array", "items": {"type": "string"}, "description": "Several links at once (one group)."},
    "engine": {"type": "string", "description": "Force an engine (e.g. http) instead of the router's choice."},
    "dest": {"type": "string", "description": "Where THIS download is saved: a sub-folder of the library root (e.g. 'Manuals') or a full path like 'D:/Games' (the person confirms a full path)."},
    "group": {"type": "string", "description": "A name for this batch."},
    "start_paused": {"type": "boolean", "description": "Queue it paused instead of starting it."},
    "start_at": {"type": "string", "description": "Hold it until a time, in the person's words: 'tomorrow at 3am', 'in 2 hours', '2026-11-02 01:30'. One time, not a repeat. Wins over start_paused."},
    "speed_limit_kbps": {"type": "integer", "description": "A speed cap for just this download, in KB/s (0 or missing = only the global limit applies)."},
    "open_when_done": {"type": "string", "enum": ["default", "off", "reveal", "folder", "open"], "description": "What to do when it finishes: nothing, show the file in its folder, open the folder, or open the file (asks the person first; programs and scripts are never opened). Default = the person's own setting."},
    "notify": {"type": "string", "enum": ["default", "on", "off"], "description": "Tell the person when this one finishes or fails (on), never (off), or follow their notification settings (default)."},
    "notify_channels": {"type": "array", "items": {"type": "string"}, "description": "Where to announce THIS download, instead of the person's usual channels, e.g. [\"discord\"] for Discord only (the notification inbox keeps its record either way). " + _notify_types_help() + " dl_prefs with no arguments lists every type (notify_options.actions). Omit to follow the person's own settings."},
    "notify_events": {"type": "array", "items": {"type": "string", "enum": ["started", "done", "failed", "stalled"]}, "description": "Which events announce THIS download, e.g. [\"done\", \"failed\"]. Omit to follow the person's own settings. A download with its own choices is announced even if notifications are off globally."},
    "dry_run": {"type": "boolean", "description": "Only say which engine would take each link; queue nothing."},
    "mode": {"type": "string", "enum": ["video", "audio"], "description": "Video pages only: video (default) or audio only."},
    "quality": {"type": "string", "description": "Video pages only: best (default), 2160p, 1440p, 1080p, 720p, 480p, 360p, or an exact 'id:<format_id>' from ytdl_formats."},
    "container": {"type": "string", "enum": ["mp4", "mkv", "webm"], "description": "Video pages only: the file type to merge into (default mp4)."},
    "video_codec": {"type": "string", "enum": ["h264", "vp9", "av1"], "description": "Video pages only: prefer this codec when there is a choice."},
    "audio_format": {"type": "string", "enum": ["mp3", "m4a", "opus", "wav", "flac", "aac", "vorbis", "alac"], "description": "Video pages, mode=audio: the audio file type (default mp3)."},
    "audio_quality": {"type": "string", "description": "Video pages, mode=audio: '0' (best) to '10', or a bitrate like '192K' (default)."},
    "embed_thumbnail": {"type": "boolean", "description": "Video pages only: embed the thumbnail as cover art."},
    "embed_metadata": {"type": "boolean", "description": "Video pages only: embed title / uploader."},
    "embed_chapters": {"type": "boolean", "description": "Video pages only: embed chapter markers."},
    "write_subs": {"type": "boolean", "description": "Video pages only: also save subtitles as separate files."},
    "embed_subs": {"type": "boolean", "description": "Video pages only: embed subtitles in the video file."},
    "sub_langs": {"type": "string", "description": "Video pages only: subtitle languages, e.g. 'en' or 'en,es'."},
    "sponsorblock_remove": {"type": "string", "description": "Video pages only (YouTube): SponsorBlock categories to cut, e.g. 'sponsor,selfpromo'."},
    "skip_if_downloaded": {"type": "boolean", "description": "Video pages only: skip a video yt-dlp's archive says was already fetched."},
    # dl_add_ytdlp's link wording and dl_add_stremio's own arguments
    "ytdlp_url": {"type": "string", "description": "The video / audio page link (YouTube or any site yt-dlp supports)."},
    "ytdlp_urls": {"type": "array", "items": {"type": "string"}, "description": "Several video links with the same options (one group)."},
    "magnet": {"type": "string", "description": "A magnet link, a 40-character infohash or a link to a .torrent file. Give this or `stream`."},
    "stream": {"type": "object", "description": "A Stremio stream object ({infoHash, fileIdx, ...}) instead of a magnet."},
    "file_index": {"type": "integer", "description": "Which file inside the torrent to download (0-based). Leave out to let Jarvis pick the main video file; same as the magnet's so=N."},
}
_CORE_NAMES = ("url", "urls", "engine", "dest", "group", "start_paused", "start_at", "dry_run")
_SHARED_NAMES = ("dest", "group", "start_paused", "start_at", "dry_run")


def _add_props(names, renamed=None):
    """One add tool's properties: `names` from _ALL_PROPS (`renamed` maps a name to the key in
    _ALL_PROPS whose text it uses), plus the per-download extras when they are advertised."""
    renamed = renamed or {}
    out = {n: dict(_ALL_PROPS[renamed.get(n, n)]) for n in names}
    if ADVERTISE_PER_DOWNLOAD_EXTRAS:
        out.update({n: dict(_ALL_PROPS[n]) for n in _EXTRA_ARGS})
    return out


TOOL_SCHEMAS = [
    {
        "name": "dl_add",
        "description": (
            "Queue a download in the Download Hub and return at once (the hub daemon does the transfer; "
            "see Menu > Downloads). Takes any link or magnet; the router picks the engine and the answer "
            "names it and the add tool for its options: dl_add_ytdlp (video / audio pages: quality, format, "
            "subtitles) or dl_add_stremio (torrents: file choice). dry_run only names the engine. "
            "To watch a video now, ytdl_download."
        ),
        "parameters": {"type": "object", "properties": _add_props(_CORE_NAMES)},
    },
    {
        "name": "dl_add_ytdlp",
        "description": (
            "Queue a video or audio download from YouTube or any site yt-dlp supports (the yt-dlp engine) in "
            "the Download Hub, in the background: quality (720p, 1080p...), mp4 / mkv / webm, audio only "
            "(mp3, m4a...), subtitles, thumbnail, chapters, SponsorBlock. One video per link. Returns at "
            "once; the hub daemon does the transfer. To watch it now, ytdl_download. dry_run only names the engine."
        ),
        "parameters": {"type": "object",
                       "properties": _add_props(("url", "urls") + _SHARED_NAMES + _VIDEO_ARGS,
                                                {"url": "ytdlp_url", "urls": "ytdlp_urls"})},
    },
    {
        "name": "dl_add_stremio",
        "description": (
            "Queue a torrent in the Download Hub with the Stremio engine: a magnet link, a 40-character "
            "infohash, a Stremio stream object or a .torrent link, optionally one chosen file of it "
            "(file_index). Needs Stremio's streaming server (stack_status / stack_start). Returns at once; "
            "the hub daemon does the transfer. dry_run only names the engine."
        ),
        "parameters": {"type": "object",
                       "properties": _add_props(("magnet", "stream", "file_index") + _SHARED_NAMES)},
    },
    {
        "name": "dl_import_legacy",
        "description": (
            "Index the videos the older ytdl_download tool already saved (in ~/.jarvis/downloads) as read-only "
            "history rows in the Download Hub, so they show up in Menu > Downloads with an Open button. "
            "Nothing is moved, renamed or deleted; running it again adds nothing twice."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "dl_providers",
        "description": (
            "List the sources of downloads the hub only watches (not its own engines): Steam installs and "
            "updates, Playnite installs, and any tool that reports its own downloads. Shows each one's mode, "
            "whether it is on, when it last reported, any error, how many rows are live or not updating, and "
            "whether it is read-only. Use for 'is the Steam download still going?' style health questions; "
            "dl_list(provider=...) has the rows themselves."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "dl_provider_prefs",
        "description": (
            "Change one provider's own choices: switch it on or off (off hides its rows and stops polling it; "
            "nothing it downloaded is touched), how often it is polled (interval_s), or its settings "
            "(e.g. {\"root\": \"D:/Steam\"} to tell the Steam provider where Steam is; "
            "{\"notify_min_update_gb\": 2} so a Steam update is announced only when it is at least 2 GB, "
            "0 = every update, default 1). The person is asked to confirm."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The provider id from dl_providers (steam, playnite, ...)."},
                "enabled": {"type": "boolean", "description": "On (true) or off (false)."},
                "interval_s": {"type": "number", "description": "Seconds between polls, 0.5 to 3600."},
                "settings": {"type": "object", "description": "Settings to set, merged key by key."},
            },
            "required": ["id"],
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
        "name": "dl_dismiss",
        "description": "Hide a Steam / Playnite / tool row that is unknown or finished from the list (one id, or finished=true for all of them). Nothing is deleted. Not for hub downloads or running ones.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "The row's id"},
            "finished": {"type": "boolean", "description": "true = every row that can be dismissed"}}},
    },
    {
        "name": "dl_schedule",
        "description": (
            "Start a download later, or change/clear when it starts. `when` is the person's own words "
            "('tomorrow at 3am', 'in 2 hours', 'tonight', '2026-11-02 01:30'); 'now' starts it at once and "
            "'clear' removes the start time (it then stays paused). Pick the download with an id, a group, an "
            "engine or all=true. One time only, not a repeat. The hub daemon does the starting, so it has to be "
            "running (Menu > Daemons). To add a download that starts later in one step use dl_add with start_at."
        ),
        "parameters": {
            "type": "object",
            "properties": dict(_SELECTOR_PROPS, when={"type": "string", "description": "When to start: plain words, 'now', or 'clear'."}),
            "required": ["when"],
        },
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
            "with an optional .level: a notifier importance level by number or name, see notify_options.levels when you "
            "read the settings), the most downloads running at once (concurrency.total; concurrency.http / "
            ".ytdlp / .stremio per engine), and connections (http.*). With `id`, "
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
    "dl_add_ytdlp": tool_dl_add_ytdlp,
    "dl_add_stremio": tool_dl_add_stremio,
    "dl_list": tool_dl_list,
    "dl_import_legacy": tool_dl_import_legacy,
    "dl_providers": tool_dl_providers,
    "dl_provider_prefs": tool_dl_provider_prefs,
    "dl_status": tool_dl_status,
    "dl_pause": tool_dl_pause,
    "dl_resume": tool_dl_resume,
    "dl_cancel": tool_dl_cancel,
    "dl_retry": tool_dl_retry,
    "dl_dismiss": tool_dl_dismiss,
    "dl_schedule": tool_dl_schedule,
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
    # "download this video" stays with ytdl_download; these are the phrases for the BACKGROUND queue.
    "dl_add_ytdlp": {
        "queue a video": 10,
        "queue the video": 10,
        "queue this video": 10,
        "download the video in the background": 10,
        "download this video in the background": 10,
        "video in the background": 9,
        "queue the audio": 9,
        "add the video to my downloads": 10,
        "add a video download": 10,
    },
    "dl_add_stremio": {
        "download this torrent": 10,
        "download a torrent": 10,
        "download the torrent": 10,
        "add a torrent": 10,
        "add this torrent": 10,
        "add this magnet": 10,
        "download this magnet": 10,
        "queue this magnet": 10,
        "queue a torrent": 10,
        "infohash": 8,
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
    "dl_import_legacy": {"import old downloads": 10, "import legacy downloads": 10, "index old downloads": 10,
                         "earlier video downloads": 9},
    "dl_providers": {"download providers": 10, "steam download": 9, "steam downloads": 9, "install progress": 9,
                     "game downloads": 8, "game install progress": 9},
    "dl_provider_prefs": {"provider settings": 9, "steam folder": 8, "turn off steam downloads": 10,
                          "poll interval": 8, "steam update notification": 9, "steam update size": 9},
    "dl_status": {"download status": 10, "download hub": 10, "downloads menu": 9},
    "dl_pause": {"pause downloads": 10, "pause the download": 10, "pause all downloads": 10},
    "dl_resume": {"resume downloads": 10, "resume the download": 10},
    "dl_cancel": {"cancel the download": 10, "cancel downloads": 10},
    "dl_retry": {"retry the download": 10, "retry downloads": 10, "retry failed downloads": 10},
    "dl_dismiss": {"dismiss the download": 10, "dismiss download": 9, "clear finished downloads": 10,
                   "clear the download list": 9, "hide the download": 9, "hide finished downloads": 9,
                   "clear unknown downloads": 9},
    "dl_schedule": {"schedule a download": 10, "schedule the download": 10, "schedule downloads": 10,
                    "start the download at": 10, "download it later": 9, "download later": 9,
                    "start downloading at": 9, "download tonight": 9, "download at night": 9},
    "dl_open": {"open the download": 9, "reveal the download": 9, "open the download folder": 10},
    "dl_settings": {"download settings": 9, "download folder": {"weight": 8, "not_with": ["open"]}},
    "dl_prefs": {"download speed limit": 10, "limit download speed": 10, "limit the download speed": 10,
                 "download notifications": 10, "notify me when downloads": 10,
                 "max concurrent downloads": 10, "downloads at once": 10, "simultaneous downloads": 10,
                 "how many downloads at once": 10, "concurrent downloads": 9,
                 "notify when the download": 10, "open when done": 9, "open when the download": 9},
    "hub_doctor": {"hub doctor": 10, "downloads aren't starting": 10, "downloads not starting": 10},
    "hub_demo_provider": {"demo downloads": 10, "downloads demo": 10},
}

TOOL_PACK_INSTRUCTION = (
    "dl_add queues and returns at once; the hub daemon does the transfer, so report progress "
    "with dl_list / dl_status instead of waiting. 'Download it tomorrow night' = dl_add with "
    "start_at (or dl_schedule for one already queued). dl_add is the router: it takes any link or "
    "magnet and its answer names the add tool for that engine. Choices that belong to a source go to "
    "its own tool: dl_add_ytdlp for a video or audio page (quality, format, audio only, subtitles; "
    "one video per link), dl_add_stremio for a torrent (magnet, infohash, stream; file_index picks a "
    "file). Do not put video options on dl_add. ytdl_download is still the tool for 'download this "
    "video now' (and with background=true it queues here too). Torrents go through the Stremio server "
    "(stack_status says whether it is up; stremio_prepare sets it up the first time). Per-download "
    "notification / speed / open-when-done choices go on the queued row with dl_prefs and its id. "
    "present_download_status(ids|group|query): after you queue with dl_add, call it ONCE with the ids dl_add "
    "returned (or the group id for a batch) to show a live card (bar, speed, Pause/Cancel, Open when done); "
    "the card updates itself, so never call it again to 'refresh'. It is read-only: it starts, stops and changes nothing."
)

# ---------------------------------------------------------------------------
# confirm gating (out-of-band, by tool name)
# ---------------------------------------------------------------------------

# cancel deletes partial files; settings can move where files go and open the private-network
# allow-list; the demo fills the owner's list with fake rows. dl_add's bulk question is asked
# inside the handler (default=False).  dl_prefs is NOT listed: it only holds the everyday keys
# (speed, notifications, folder names inside the library), and the one choice that runs
# something unseen (open-the-file when done) asks inside the handler, default=False.
TOOL_CONFIRM_REQUIRED = {"dl_cancel", "dl_settings", "dl_provider_prefs", "hub_demo_provider"}

# The Ask box's "/" palette commands (/dl, /dls, /dlpause ...): typed twins of the tools above.  They
# live in download_hub_commands.py (data only) to keep this file readable; if that module ever fails
# to import the tools still load, just without the commands.
try:
    from .download_hub_commands import HUB_COMMANDS as TOOL_COMMANDS
except Exception:  # noqa: BLE001
    TOOL_COMMANDS = []
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
    "dl_providers": {
        "list_item_drop": {"providers": {
            "medium": ["icon", "source", "poll_tool", "last_ok_age_s"],
            "low": ["icon", "source", "poll_tool", "last_ok_age_s", "controls", "capabilities", "interval_s", "kind"]}},
        "list_item_truncate": {"providers": {"last_error": {"medium": 160, "low": 80}}},
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
        "does": "The Download Hub's router: queues a link (any engine) and names the engine and the add tool for its options; a direct http(s) link downloads in the background.",
        "steps": [
            {"run": {"url": "https://<a real file link>", "dry_run": True},
             "expect": "ok: true, one decision: engine http, reason 'direct link (HTTP engine)', nothing queued."},
            {"run": {"url": "https://<a real, small file link>"},
             "expect": "ok: true, one item added with an id; it appears in Menu > Downloads and finishes (daemon running)."},
            {"run": {"url": "http://192.168.1.1/"},
             "expect": "ok: false, problem says it points at a private or local address (refused by the SSRF guard)."},
            {"run": {"url": "https://<a real file link>", "dest": "../../etc"},
             "expect": "ok: false: dest must be a sub-folder of the library root (or a full path), not '..'; nothing queued."},
            {"run": {"url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", "dry_run": True},
             "expect": "ok: true, one decision: engine stremio, reason 'magnet link (Stremio engine)' with Stremio's server running; with it stopped, the decision's problem says the stremio engine isn't ready and how to start it (stack_start). Nothing queued either way."},
            {"run": {"url": "https://<a real, small file link>", "start_at": "in 2 minutes"},
             "expect": "ok: true, the item is paused with start_at set; Menu > Downloads shows 'Scheduled' and about two minutes later it starts by itself (daemon running)."},
            {"run": {"url": "https://<a real, small file link>", "start_at": "2020-01-01 09:00"},
             "expect": "ok: false: '2020-01-01T09:00:00 has already passed; give a time in the future'; nothing queued."},
            {"run": {"url": "https://www.youtube.com/watch?v=<a short video id>", "dry_run": True},
             "expect": "ok: true, one decision: engine ytdlp, reason 'YouTube page', add_tool dl_add_ytdlp (a magnet link says dl_add_stremio, a plain file link dl_add). (Without yt-dlp / ffmpeg the call is still ok: true, but the decision is ok: false and its problem says the ytdlp engine isn't ready and what is missing.)"},
            {"run": {"url": "https://www.youtube.com/watch?v=<a short video id>", "mode": "audio"},
             "expect": "Still works (deprecated): ok: true, one item (engine ytdlp, kind audio) and a note that mode on dl_add is deprecated and dl_add_ytdlp is the tool for it. (Without yt-dlp: ok: false, the problem says yt-dlp isn't installed, and the same deprecation note.)"},
            {"run": {},
             "expect": "No error, a question: needs_clarification 'Which link should I download?'; nothing is queued."},
            {"run": {"url": "ftp://<any host>/<a file>"},
             "expect": "ok: false, 'that doesn't look like something I can download: expected an http(s) link ...'; nothing queued. (⚠️ The sentence still ends '(magnet links arrive with a later phase)', which is out of date: magnets work through dl_add today. Listed under Open points in the test report.)"},
            {"run": {"url": "<the full path of a small .torrent file on this PC>", "dry_run": True},
             "expect": "ok: true, one decision: engine stremio, reason '.torrent file on this PC (Stremio engine)', add_tool dl_add_stremio. With Stremio's server stopped the decision's problem says the stremio engine isn't ready and how to start it (stack_start). Nothing is queued, but a copy of the file is kept at ~/.jarvis/hub/torrents/<infohash>.torrent even on a dry run. (Seen on Linux with a hand-built .torrent and no server; a real server has never been given a .torrent by this engine.)"},
            {"run": {"url": "<the full path of a folder, a .torrent file that does not exist, or a text file renamed .torrent>"},
             "expect": "ok: false, nothing queued. The sentence is the router's general one ('that doesn't look like something I can download: expected an http(s) link ...'), not one about the file: a known gap (Open points of the brief 22 report)."},
            {"run": {"urls": ["https://<link 1>", "https://<link 2>", "https://<link 3>", "https://<link 4>", "https://<link 5>", "https://<link 6>"]},
             "expect": "Six links is over the limit of 5 without asking: it asks to confirm first. Answer Don't: ok: false, cancelled, 'not confirmed: nothing was queued'. Answer Yes: six items queued."},
            {"ask": "Download this file: https://<a real, small file link>",
             "expect": "Calls dl_add (not ytdl_download) and says it was queued."},
            {"ask": "/dl https://<a real, small file link>",
             "expect": "The palette command: shows 'dl' with the link filled in, asks to confirm (caution), then queues it exactly like the tool call; it appears in Menu > Downloads."},
            {"ask": "/dl",
             "expect": "Refuses before running anything: it needs a link."},
            {"run": {"url": _LOCAL},
             "expect": "With the local test server running but allow_private_hosts not set yet: ok: false, refused as a private address. After dl_settings sets allow_private_hosts to ['127.0.0.1:<port>'] (see dl_settings) the same call queues and downloads it."},
        ],
        "needs": ["The 'Download hub' daemon running (Menu > Daemons)", "Internet access for the real-link steps",
                  "yt-dlp and ffmpeg installed for the video steps",
                  "For the local-server step: a folder with a file in it and `python -m http.server <port>` running there; <port> and <file> in the step are that port and file name"],
        "care": "Writes the file under the library root (default ~/Downloads/Jarvis/Files). A local test server needs allow_private_hosts set first (dl_settings).",
        "watch": ["Adding the same link twice returns the existing item (duplicate: true), not a second download.",
                  "⚠️ start_at \"yesterday at 9am\" is not refused: the time reader takes it as the NEXT 9 am (tomorrow) and the item is queued paused for then (seen when this entry was checked). Use an exact past date, as in the step above, to see the refusal. Listed under Open points in the test report.",
                  "More than 5 links in one call asks for confirmation first.",
                  "A .torrent file on this PC goes through dl_add (as the path), not through dl_add_stremio. The file must exist, be a real regular file under 5 MB (not a link, not a network share) and be given as a full path. ⚠️ The row's source is a magnet made from the file's own name, but the path you typed may still show in the answer and as the row's title until the run starts (brief 22 report, Open point 1); never seen with a real server.",
                  "From a remote chat guest (Discord / Instagram) dl_list answers but dl_add, dl_add_ytdlp and dl_add_stremio are refused: adding downloads is the owner's.",
                  "Each added row in the answer carries add_tool, the tool for that engine's own options.",
                  "Kill the daemon mid-file (Menu > Daemons > Stop), start it again: the row goes back to queued and the file resumes, not restarts (needs a server that supports Range)."],
    },
    "dl_add_ytdlp": {
        "does": "Queues a video or audio page in the Download Hub with the yt-dlp engine and its own options (quality, container, audio only, subtitles...); the daemon downloads it in the background.",
        "steps": [
            {"run": {"url": "https://www.youtube.com/watch?v=<a short video id>", "dry_run": True},
             "expect": "ok: true, one decision: engine ytdlp, add_tool dl_add_ytdlp, nothing queued. (Without yt-dlp / ffmpeg the call is still ok: true, but the decision is ok: false and its problem says the ytdlp engine isn't ready and what is missing.)"},
            {"run": {"url": "https://www.youtube.com/watch?v=<a short video id>", "mode": "audio", "audio_format": "mp3"},
             "expect": "ok: true, one item (engine ytdlp, kind audio); it shows an indeterminate bar with growing size, then lands as an .mp3 in the Audio folder. (Without yt-dlp: ok: false, the skipped problem says yt-dlp isn't installed and nothing is queued.)"},
            {"run": {"url": "https://www.youtube.com/watch?v=<a short video id>", "quality": "1080p", "container": "gif"},
             "expect": "ok: false: container must be one of mp4, mkv, webm (nothing is queued)."},
            {"run": {"url": "https://<a real file link>.zip", "dry_run": True},
             "expect": "ok: true for the call, with one decision that is ok: false and whose problem says 'the ytdlp engine can't take that input'; a plain file link belongs to dl_add."},
            {"run": {},
             "expect": "No error, a question: needs_clarification 'Which video link should I download?'; nothing is queued."},
            {"ask": "Queue this video in the background as an mp3: https://www.youtube.com/watch?v=<a short video id>",
             "expect": "Finds and calls dl_add_ytdlp (not dl_add with video options, not ytdl_download) with mode audio; says it was queued."},
        ],
        "needs": ["The 'Download hub' daemon running (Menu > Daemons)", "Internet access", "yt-dlp and ffmpeg installed"],
        "care": "Writes the file under the library root (Video / Audio folder).",
        "watch": ["From a remote chat guest the tool is refused: adding downloads is the owner's.",
                  "⚠️ Playlists: the code for 'a playlist link asks first, playlist: all queues one group of at most 50' exists (hub/engines/_ytdlp_playlist.py, brief 21) but this tool is not hooked to it in jarvis-main_164_: its schema has no `playlist` or `generic` argument, and a playlist link is not asked about or made into a group. If Menu > Debug shows `playlist` in this tool's arguments, the hook has been merged: then check that a playlist link queues nothing and asks (title, size, five titles), and that playlist: all queues one group of up to 50. `generic` is switched off in this version."],
    },
    "dl_add_stremio": {
        "does": "Queues a torrent (magnet, infohash, Stremio stream or .torrent link), optionally one chosen file of it, with the Stremio engine.",
        "steps": [
            {"run": {"magnet": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", "dry_run": True},
             "expect": "ok: true, one decision: engine stremio, add_tool dl_add_stremio. With Stremio's server stopped the decision's problem says so and how to start it (stack_start). Nothing queued."},
            {"run": {"magnet": "0123456789abcdef0123456789abcdef01234567", "file_index": 2, "dry_run": True},
             "expect": "ok: true, one decision for a magnet that carries so=2 (an infohash alone is accepted)."},
            {"run": {"magnet": "not a magnet"},
             "expect": "ok: false: that is not a magnet link, a 40-character infohash, a Stremio stream or a .torrent link."},
            {"run": {"magnet": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", "stream": {"infoHash": "0123456789abcdef0123456789abcdef01234567"}},
             "expect": "ok: false: give a magnet or a stream, not both."},
            {"run": {"magnet": "https://<a real .torrent link>.torrent", "file_index": 1},
             "expect": "ok: false: a .torrent link can't carry a file choice; leave file_index out."},
            {"run": {"magnet": "<the full path of a .torrent file on this PC>"},
             "expect": "ok: false: that is not a magnet link, a 40-character infohash, a Stremio stream or a .torrent link. A file on this PC goes through dl_add instead (see dl_add), by design (brief 22 report, Open point 3)."},
            {"run": {},
             "expect": "No error, a question: needs_clarification 'Which magnet link, infohash or stream should I download?'; nothing is queued."},
            {"ask": "Download this magnet, only file 3: magnet:?xt=urn:btih:<a real infohash>",
             "expect": "Calls dl_add_stremio with file_index 3 and says it was queued (Stremio's server running)."},
        ],
        "needs": ["The 'Download hub' daemon running (Menu > Daemons)", "Stremio's streaming server for the queueing step (stack_start)", "A magnet you are allowed to download"],
        "care": "Writes the torrent's files under the library root.",
        "watch": ["From a remote chat guest the tool is refused: adding downloads is the owner's."],
    },
    "dl_import_legacy": {
        "does": "Indexes the videos the older ytdl_download tool saved as read-only rows (provider 'Earlier video downloads'); nothing is moved or deleted.",
        "steps": [
            {"run": {}, "expect": "ok: true with found / imported / already counts. With no dl_* folders in ~/.jarvis/downloads (nothing to import is the failure case here): found 0, imported 0 and the note 'No earlier ytdl_download folders were found.'"},
            {"ask": "Import my old video downloads into the hub", "expect": "Calls dl_import_legacy and reports how many it found; asking again imports 0 and says they were already there (nothing is added twice)."},
        ],
        "watch": ["Each row is done, has Open / Reveal only, and no pause / cancel / retry button.",
                  "The old ytdl_download tool still prunes its own folders; a pruned file's row says the file is gone when opened."],
    },
    "dl_providers": {
        "does": "Lists the watched sources (Steam, Playnite, tools with their own downloads): mode, on / off, last report, last error, live and not-updating rows, read-only or not.",
        "steps": [
            {"run": {}, "expect": "ok: true with a providers list; with Steam installed there is a 'steam' entry (mode poll, read_only true); after playnite_install_game a 'playnite' one. Before the hub daemon has ever run (or with no Steam / Playnite here and no tool declaring downloads) it is still ok: true but providers is [] and count 0: nothing is registered until the daemon runs, and that is not an error."},
            {"ask": "Is my Steam download still going?", "expect": "Calls dl_providers or dl_list(provider=steam) and answers from the rows' percent / state."},
            {"ask": "/dlproviders",
             "expect": "The palette command: runs at once (safe, no confirmation) and lists the same providers. The steam entry carries settings {notify_min_update_gb: 1.0} on a fresh install, and never Steam's folder."},
        ],
        "needs": ["The Download hub daemon running: it polls Steam and registers providers"],
        "watch": ["Steam closed: the steam entry shows an error ('Steam isn't running ...') and its rows go grey (stale), never red.",
                  "A provider switched off disappears from dl_list until switched back on."],
    },
    "dl_provider_prefs": {
        "does": "Switches a provider on or off, changes how often it is polled, or sets its settings (for Steam, where Steam is installed).",
        "steps": [
            {"run": {"id": "steam", "settings": {"root": "D:/Steam"}}, "expect": "Asks to confirm, then ok: true; the next poll uses that folder. Without a steam provider (Steam not installed, daemon never run) it is ok: false, 'no provider called 'steam' (see dl_providers)'."},
            {"run": {"id": "steam", "enabled": False}, "expect": "Asks to confirm, then ok: true; Steam rows vanish from dl_list and Menu > Downloads."},
            {"run": {"id": "nope", "enabled": True}, "expect": "ok: false: no provider called 'nope' (see dl_providers)."},
            {"run": {"id": "steam"}, "expect": "ok: false: 'say what to change: enabled, interval_s or settings'."},
            {"run": {"id": "steam", "interval_s": 0}, "expect": "ok: false: interval_s must be a number of seconds from 0.5 to 3600."},
            {"run": {"id": "steam", "settings": {"notify_min_update_gb": 2}},
             "expect": "Asks to confirm, then ok: true; dl_providers shows steam settings notify_min_update_gb 2.0 and Steam's folder setting is untouched. From now a Steam update under 2 GB finishes silently."},
            {"run": {"id": "steam", "settings": {"notify_min_update_gb": 0}},
             "expect": "ok: true; every Steam update is announced from now on, however small."},
            {"run": {"id": "steam", "settings": {"notify_min_update_gb": -1}},
             "expect": "ok: false: notify_min_update_gb must be between 0 and 10000 GB; nothing in the call was applied."},
            {"run": {"id": "steam", "settings": {"notify_min_update_gb": "lots"}},
             "expect": "ok: false: notify_min_update_gb must be a number of GB (0 = every update)."},
            {"run": {"id": "playnite", "settings": {"notify_min_update_gb": 1}},
             "expect": "ok: false: that is a steam setting; playnite doesn't have it (never a silent no-op)."},
        ],
        "needs": ["The Download hub daemon running",
                  "For the notification steps without a real Steam update: Steam running, and a sample library. Make a folder S with a sub-folder steamapps containing appmanifest_1001.acf: \"AppState\" { \"appid\" \"1001\" \"name\" \"Sample Big Update\" \"installdir\" \"SampleBig\" \"StateFlags\" \"258\" \"LastUpdated\" \"1700000000\" \"SizeOnDisk\" \"5000000000\" \"BytesToDownload\" \"3221225472\" \"BytesDownloaded\" \"1073741824\" } (one pair per line), then set the steam provider's root to S."],
        "care": "Switching a provider off hides its rows and stops polling it; it never touches what the provider downloaded.",
        "watch": ["Sample update (3 GB, default threshold 1): within a poll the row shows in Menu > Downloads as an update. Change StateFlags to \"4\" in the .acf: the next poll marks it done and one 'Game updated: Sample Big Update finished updating (3.0 GB)' arrives, once, also after restarting the daemon.",
                  "Same sample with notify_min_update_gb set to 5: finishing it sends nothing, and the 'all downloads finished' summary doesn't count it.",
                  "Edit the sample to \"BytesToDownload\" \"209715200\" (200 MB) at the default: finishing it is silent. Set the threshold to 0 and repeat with appid 1002: announced.",
                  "A new game install (a manifest with no LastUpdated and SizeOnDisk 0) finishes with 'finished installing', however small, and follows the Notifications panel's 'download finished' switch, not the threshold.",
                  "With Notifications > 'download finished' off, not even a big Steam update is announced."],
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
            {"run": {"search": "<text that matches no title>"},
             "expect": "ok: true, items [], count 0: nothing found is an empty list, not an error (an unknown provider name does the same)."},
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
            {"run": {"id": "no-such-id"}, "expect": "ok: false, 'no such download'."},
            {"ask": "Is the download hub running?", "expect": "Says whether the daemon is alive."},
            {"ask": "/dls",
             "expect": "The palette command: runs at once (safe) and shows the same totals as the tool call. '/dls <id>' shows that one download."},
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
            {"run": {"id": "no-such-id"},
             "expect": "ok: false, 'no such download'; nothing changes."},
            {"ask": "/dlpause <a downloading id>",
             "expect": "The palette command: asks to confirm (caution), then pauses just that one."},
            {"ask": "/dlpause",
             "expect": "Refuses before running anything: it needs an id, so a bare /dlpause can never pause the whole queue."},
            {"ask": "/dlpauseall",
             "expect": "Asks to confirm, then pauses every running download (the same as dl_pause with all=true)."},
        ],
        "watch": ["Pausing a row from a read-only provider (a Steam or Playnite row) says it is read-only, never fails silently."],
    },
    "dl_resume": {
        "does": "Resumes paused downloads from where they stopped.",
        "steps": [
            {"run": {"id": "<a paused id>"}, "expect": "ok: true, the row goes to queued and then downloads again from its byte offset."},
            {"run": {"all": True}, "expect": "With rows in the hub: ok: true; rows that weren't paused are listed under skipped, not an error. With an empty hub (the failure case): ok: false, 'no such download'."},
            {"run": {"id": "no-such-id"}, "expect": "ok: false, 'no such download'."},
            {"ask": "/dlresume <a paused id>", "expect": "The palette command: asks to confirm, then resumes just that one."},
            {"ask": "/dlresumeall", "expect": "Asks to confirm, then resumes every paused download."},
        ],
    },
    "dl_cancel": {
        "does": "Cancels downloads and deletes their .part files; finished files are never touched.",
        "steps": [
            {"run": {"id": "<a downloading or paused id>"},
             "expect": "Asks to confirm, then ok: true; the row is cancelled and its .part file is gone."},
            {"run": {"id": "<a done id>"}, "expect": "ok: false, 'already done'; the finished file is still there."},
            {"run": {"id": "no-such-id"}, "expect": "ok: false, 'no such download' (asks to confirm first, like every cancel); nothing is deleted."},
        ],
        "care": "Deletes the partial file of what you cancel.",
    },
    "dl_dismiss": {
        "does": "Hides a Steam / Playnite / tool row that is unknown or finished from the list. Nothing is deleted and no file is touched.",
        "steps": [
            {"run": {"id": "<an unknown or done Steam / Playnite row>"},
             "expect": "ok: true, the id is in 'dismissed'; the row is gone from the list and the counts. No confirmation is asked."},
            {"run": {"id": "<a downloading provider row>"},
             "expect": "ok: false, 'it is still running'; the row stays."},
            {"run": {"id": "<a hub download id>"},
             "expect": "ok: false, a hub download isn't dismissed: cancel it with dl_cancel."},
            {"run": {"finished": True},
             "expect": "ok: true with a count: every unknown or finished provider row is hidden; hub downloads and running rows stay."},
            {"run": {}, "expect": "ok: false asking for an id or finished: true."},
            {"run": {"id": "no-such-id"}, "expect": "ok: false, 'no such download'."},
        ],
        "needs": ["A Steam / Playnite / tool row that is unknown or finished"],
        "watch": ["A hidden row comes back by itself if its provider reports it live again (the game starts downloading again).",
                  "From a remote chat guest the tool is refused (and not even offered)."],
        "care": "Hides rows only; nothing is deleted.",
    },
    "dl_retry": {
        "does": "Puts failed, stalled or cancelled downloads back in the queue with a fresh retry budget.",
        "steps": [
            {"run": {"id": "<a failed id>"}, "expect": "ok: true, the row goes to queued and starts again."},
            {"run": {"id": "<a downloading id>"}, "expect": "ok: false, 'nothing to retry (state: downloading)'; the download goes on."},
            {"run": {"id": "no-such-id"}, "expect": "ok: false, 'no such download'."},
        ],
    },
    "dl_schedule": {
        "does": "Starts a download later (plain words like 'tomorrow at 3am'), changes that time, or clears it; the same time grammar as scheduling a daemon.",
        "steps": [
            {"run": {"id": "<a queued or paused id>", "when": "in 2 minutes"},
             "expect": "ok: true, start_at is the time two minutes ahead; the row shows 'Scheduled' (paused) and starts by itself at that time."},
            {"run": {"id": "<the same id>", "when": "clear"},
             "expect": "ok: true, 'start time cleared'; the row stays paused until you resume it."},
            {"run": {"id": "<the same id>", "when": "now"},
             "expect": "ok: true; the row goes to queued and starts at once."},
            {"run": {"id": "<a downloading id>", "when": "tomorrow at 3am"},
             "expect": "ok: false: it is already downloading, pause it first."},
            {"run": {"id": "<any id>", "when": "every night"},
             "expect": "ok: false: a download starts once; give a single time."},
            {"run": {"id": "<any id>", "when": "last week"},
             "expect": "ok: false: \"couldn't understand 'last week' as a start time\" and the forms it does read ('in 20 minutes', 'tomorrow at 9am', an exact '2026-09-16 14:30'). An exact date in the past says '... has already passed; give a time in the future'."},
            {"run": {"id": "no-such-id", "when": "in 5 minutes"}, "expect": "ok: false, 'no such download'."},
            {"ask": "Download that file tomorrow at 3am", "expect": "Calls dl_add with start_at (or dl_schedule) and says when it will start."},
        ],
        "needs": ["The 'Download hub' daemon running (Menu > Daemons) at the start time"],
        "watch": ["Stop the daemon, wait past the start time, start it again: the download starts right away.",
                  "Pressing Resume on a scheduled download starts it now and drops the schedule."],
    },
    "dl_open": {
        "does": "Opens a finished download, or reveals it in the file manager; only inside the library folder.",
        "steps": [
            {"run": {"id": "<a done id>", "action": "reveal"}, "expect": "ok: true and no `note`; the folder opens with the file selected (a `note` means the folder was opened without it)."},
            {"run": {"id": "<a done id>"}, "expect": "ok: true; the file opens in its default program."},
            {"run": {"id": "<a queued id>"}, "expect": "ok: false, that download has no file yet."},
            {"run": {"id": "no-such-id"}, "expect": "ok: false, 'no such download'; no program is started."},
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
            {"run": {"set": {"concurrency.http": 999}},
             "expect": "ok: false: 'concurrency.http must be between 1 and 16'; nothing is changed."},
            {"run": {"set": {"allow_private_hosts": ["127.0.0.1:<port>"]}},
             "expect": "Asks to confirm, then ok: true; the dl_add local-server step now works against that port only, and a neighbouring port is still refused."},
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
            {"run": {"set": {"rate_limit_kbps": "fast"}},
             "expect": "ok: false: 'rate_limit_kbps must be a whole number'; the limit is unchanged."},
            {"run": {"set": {"after_download.open": "launch-missiles"}},
             "expect": "ok: false: 'after_download.open must be one of: off, reveal, folder, open'."},
            {"run": {"set": {"schedule": [{"days": "all", "from": "25:00", "to": "23:59", "rate_limit_kbps": 1}]}},
             "expect": "ok: false: 'schedule window 1: from and to must be times like 01:00'; the schedule is unchanged."},
            {"run": {"test_notification": "bogus"},
             "expect": "ok: false: 'event must be one of: started, done, failed, stalled, group_done, queue_empty'."},
            {"run": {"id": "no-such-id", "set": {"limit_kbps": 300}},
             "expect": "ok: false, 'no such download'."},
            {"run": {"set": {"unknown_settle_hours": 48}},
             "expect": "ok: true, changed ['unknown_settle_hours']: a Steam / Playnite row that vanished from its program's list now settles to 'cancelled' after 48 hours instead of 24. Set it back to 24 afterwards."},
            {"run": {"set": {"unknown_settle_hours": 0}},
             "expect": "ok: false: 'unknown_settle_hours must be between 1 and 720'; the value is unchanged."},
            {"run": {"set": {"unknown_settle_hours": "soon"}},
             "expect": "ok: false: 'unknown_settle_hours must be a whole number'."},
            {"ask": "Limit my downloads to 1 MB/s", "expect": "Calls dl_prefs with rate_limit_kbps 1024 and confirms the new limit."},
        ],
        "care": "Opening finished files automatically is off by default. Programs and scripts (.exe, .bat, .ps1, .js, .lnk, ...) are only ever shown in their folder, never opened.",
        "watch": ["A burst of finishes inside the 'group within N seconds' window arrives as one notification ('3 downloads finished').",
                  "A batch of many files never opens more than a few windows at once."],
    },
    "hub_doctor": {
        "does": "Checks the store, the daemon, the library folder and disk, each engine and provider, and any spooled reports.",
        "steps": [
            {"run": {}, "expect": "ok: true (or a clear problem) with a list of checks; the daemon row says running when the hub daemon is up. With the daemon stopped (Menu > Daemons > Stop; the failure case) it still answers ok: true, the summary reads 'working, with N warning(s)' and names daemon, and the daemon check says 'not running: nothing will start until it is (Menu > Daemons > Download hub)'. Engines you have not set up (qBittorrent, Stremio's server, yt-dlp) show as warnings too, not failures."},
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
            {"run": {"action": "bogus"},
             "expect": "ok: false: 'action must be start, stop, clear or status'; nothing changes."},
        ],
        "needs": ["The Download hub daemon running (it advances the demo rows)"],
        "watch": ["After about 40 s the 'silent' row goes grey with 'last update ... ago' (stale), not red.",
                  "Demo rows show Pause / Resume / Cancel as their state allows (after Pause the row shows paused; the 'silent' row is the stale one)."],
    },
}
