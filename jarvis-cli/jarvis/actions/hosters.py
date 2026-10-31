"""File-host tools (Download Hub, link-resolver plan): dl_add_hoster, hoster_resolve, hoster_doctor.

`dl_add` stays the router and already sends a MediaFire, Google Drive or Buzzheavier page link to the hosters
engine (hub/engines/hosters.py); nothing here is needed for the everyday "download this link".  These are the
extras:

  dl_add_hoster   queue file-host links FORCING the hosters engine, so a link no resolver takes is refused with a
                  reason instead of being routed away quietly.  Same shared `_queue` as every add tool.
  hoster_resolve  says what a link would do (which resolver, how it works, how sure the plan is, what the last
                  `hoster_doctor` run saw) and, with probe on, really asks the host and reports the file name, size
                  and any wait.  Downloads nothing.  The file URL is never printed in full (it carries a token):
                  only its host.
  hoster_doctor   runs each resolver against a known-good, harmless link the owner chose for it
                  (`hosters.canary.<id>`) and records what it saw as `hosters.health.<id>`.  `last_verified` is
                  therefore always something that was observed, never a claim made in advance.

Owner only: `hoster_resolve` and `hoster_doctor` contact third-party sites from the owner's PC, so a chat guest is
refused in the handler and tools.py keeps them out of a guest's tool list (OWNER_ONLY_TOOLS); `dl_add_hoster` is
hidden from a guest by its dl_add_ prefix.  No TOOL_PACK_INSTRUCTION here on purpose: the "downloads" group has ONE,
in download_hub.py.
"""

import datetime
from urllib.parse import urlsplit

TOOL_GROUP = "downloads"

# "download this link" stays with dl_add; these phrases name a file host or ask about the resolvers themselves.
TOOL_KEYWORDS = {
    "dl_add_hoster": {
        "download from mediafire": 9,
        "download from google drive": 9,
        "download from buzzheavier": 9,
        "mediafire link": 8,
        "google drive link": 8,
        "buzzheavier link": 8,
        "file host link": 8,
    },
    "hoster_resolve": {
        "what would this link do": 10,
        "resolve this link": 10,
        "check this file host link": 10,
        "is this file host link supported": 10,
        "which file hosts are supported": 9,
        "supported file hosts": 9,
        "file host resolver": 9,
    },
    "hoster_doctor": {
        "hoster doctor": 10,
        "are the file hosts working": 10,
        "check the file host resolvers": 10,
        "test the file host links": 9,
        "file hosts broken": 9,
        "mediafire stopped working": 9,
    },
}

_LINK_PROPS = {
    "url": {"type": "string", "description": "A file-host link."},
    "urls": {"type": "array", "items": {"type": "string"}, "description": "Several file-host links."},
}

TOOL_SCHEMAS = [
    {
        "name": "dl_add_hoster",
        "description": (
            "Queue links to file-hosting sites (MediaFire, Google Drive, Buzzheavier) in the Download Hub with the "
            "hosters engine, which finds the real file link on the host's page and then downloads it (pause, resume "
            "and progress work as usual). dl_add also takes these links; use this one to be sure the hosters engine "
            "is used. A link no resolver handles (a folder, an unknown site) is refused with the reason. Returns at "
            "once; the hub daemon does the transfer. dry_run only names the engine."
        ),
        "parameters": {
            "type": "object",
            "properties": dict(_LINK_PROPS, **{
                "dest": {"type": "string",
                         "description": ("Where THIS download is saved: a sub-folder of the library root (e.g. "
                                         "'Mods') or a full path like 'D:/Games' (the person confirms a full path).")},
                "group": {"type": "string", "description": "A name for this batch."},
                "start_paused": {"type": "boolean", "description": "Queue it paused instead of starting it."},
                "start_at": {"type": "string",
                             "description": ("Hold it until a time, in the person's words: 'tomorrow at 3am', 'in 2 "
                                             "hours'. One time, not a repeat. Wins over start_paused.")},
                "dry_run": {"type": "boolean", "description": "Only say which engine would take each link; queue nothing."},
            }),
        },
    },
    {
        "name": "hoster_resolve",
        "description": (
            "Say what the Download Hub would do with file-host links, without downloading anything: which resolver "
            "takes each link, how it works, how reliable that method is and what the last hoster_doctor run saw. "
            "With probe on (the default) it also asks the host and reports the file name, size, and any wait or "
            "limit it meets; the file address itself is never shown. With no link it lists the supported hosts."
        ),
        "parameters": {
            "type": "object",
            "properties": dict(_LINK_PROPS, **{
                "probe": {"type": "boolean",
                          "description": "Really ask the host (default true). False only describes the resolver."},
            }),
        },
    },
    {
        "name": "hoster_doctor",
        "description": (
            "Check that the file-host resolvers still work: for each one that has a canary link (a known-good, "
            "harmless file the person chose with dl_settings hosters.canary) it resolves it and records whether it "
            "worked, so a site that changed shows up here and not as a mystery failure. Downloads nothing. Also "
            "reports resolvers that failed to load."
        ),
        "parameters": {
            "type": "object",
            "properties": {"host": {"type": "string",
                                    "description": "One resolver id (mediafire, gdrive, buzzheavier). Leave out for all."}},
        },
    },
]

MAX_PROBES = 5


def _hub():
    from . import download_hub
    return download_hub


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def tool_dl_add_hoster(args):
    """Queue file-host links with the hosters engine forced."""
    try:
        args = args or {}
        hub = _hub()
        if hub._is_chat_guest():
            return hub._guest_refusal()
        urls = hub._gather_urls(args)
        if not urls:
            return {"needs_clarification": True, "message": "Which file-host link should I download?"}
        return hub._queue(args, urls, "hosters", None, "dl_add_hoster")
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return {"ok": False, "error": "dl_add_hoster failed: %s" % exc}


def _outcome_of(resolver, url, settings):
    """Run one resolve and say what happened in the owner's terms.  Never raises, never prints the file URL."""
    from ..hub import hosters, netguard
    from ..hub.hosters import base as hb
    out = {}
    ctx = hosters.make_context(settings)
    try:
        result = resolver.resolve(url, ctx)
    except hb.NeedsWait as exc:
        return {"status": "wait", "wait_seconds": exc.seconds, "note": exc.message or "the host asks for a wait"}
    except hb.QuotaExceeded as exc:
        return {"status": "quota", "retry_after_seconds": exc.retry_after_s, "note": exc.message}
    except hb.NeedsHuman as exc:
        return {"status": "needs_you", "note": exc.reason}
    except hb.Dead as exc:
        return {"status": "dead", "note": exc.message}
    except hb.Unsupported as exc:
        return {"status": "unsupported", "note": exc.message}
    except hb.Transient as exc:
        return {"status": "temporary_problem", "note": exc.message}
    except hb.ResolveFailed as exc:
        return {"status": "page_not_understood", "note": exc.message}
    except netguard.GuardError as exc:
        return {"status": "refused", "note": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "note": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
    finally:
        ctx.fetch.close()
    items = result if isinstance(result, (list, tuple)) else [result]
    first = items[0] if items else None
    out["status"] = "ok"
    out["kind"] = "list" if isinstance(result, (list, tuple)) else "file"
    out["files"] = len(items)
    if first is not None and getattr(first, "url", ""):
        out["file_host"] = urlsplit(first.url).hostname or ""      # the host only: the address carries a token
        out["filename"] = first.filename
        out["size_bytes"] = first.size
        out["sends_headers"] = sorted(first.headers)
    elif first is not None and first.filename:
        out["filename"] = first.filename
    return out


def tool_hoster_resolve(args):
    """What would the hub do with these file-host links?  Optionally asks the host.  Downloads nothing."""
    try:
        args = args or {}
        hub = _hub()
        if hub._is_chat_guest():
            return hub._guest_refusal()
        from ..hub import hosters, router
        svc = hub._svc()
        settings = svc._store(None).get_settings()
        urls = hub._gather_urls(args)
        if not urls:
            return {"ok": True, "supported": [dict(r.describe(), enabled=hosters.is_enabled(r.id, settings),
                                                    hosts=hosters.hosts_of(r, settings),
                                                    last_check=hosters.health(settings, r.id))
                                               for r in hosters.resolvers()],
                    "load_errors": dict(hosters.LOAD_ERRORS)}
        probe = args.get("probe") is not False
        results = []
        for text in urls[:MAX_PROBES]:
            resolver = hosters.find(text, settings)
            entry = {"input": text[:120]}
            if resolver is None:
                decision = router.route(text, settings=settings)
                entry.update(handled=False, would_use=decision.get("engine"),
                             note="no file-host resolver takes this link (it may be a direct link, a disabled "
                                  "resolver, or a host that isn't supported)")
            else:
                entry.update(handled=True, resolver=resolver.id, label=resolver.label, method=resolver.method,
                             confidence=resolver.confidence, needs_human=resolver.needs_human,
                             notes=resolver.notes, last_check=hosters.health(settings, resolver.id))
                if probe:
                    entry["probe"] = _outcome_of(resolver, text, settings)
            results.append(entry)
        out = {"ok": True, "links": results}
        if len(urls) > MAX_PROBES:
            out["note"] = "Only the first %d links were looked at." % MAX_PROBES
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "hoster_resolve failed: %s" % exc}


def tool_hoster_doctor(args):
    """Resolve each resolver's canary link and record what was seen."""
    try:
        args = args or {}
        hub = _hub()
        if hub._is_chat_guest():
            return hub._guest_refusal()
        from ..hub import hosters
        store = hub._svc()._store(None)
        settings = store.get_settings()
        wanted = str(args.get("host") or "").strip().lower()
        if wanted and hosters.get(wanted) is None:
            return {"ok": False, "error": "unknown file host %r (known: %s)" % (wanted[:30], ", ".join(hosters.ids()))}
        canary = (settings.get("hosters") or {}).get("canary") or {}
        report = []
        for resolver in hosters.resolvers():
            if wanted and resolver.id != wanted:
                continue
            row = {"resolver": resolver.id, "label": resolver.label}
            if not hosters.is_enabled(resolver.id, settings):
                row["status"] = "disabled"
            elif not canary.get(resolver.id):
                row["status"] = "no_canary"
                row["note"] = ("no test link set: give dl_settings hosters.canary {%s: <a known-good, harmless file "
                               "link>} to check it" % resolver.id)
            else:
                seen = _outcome_of(resolver, canary[resolver.id], settings)
                status = seen.get("status")
                # A wait or a quota means the flow ran to its last step, so the page was understood.
                good = True if status in ("ok", "wait", "quota") else (None if status in ("needs_you",
                                                                                          "temporary_problem") else False)
                row.update(status=status, working=good, note=seen.get("note") or "", at=_now())
                if status == "dead":
                    row["note"] = (row["note"] + " (the canary link itself looks dead: pick another)").strip()
                store.set_settings({"hosters.health.%s" % resolver.id:
                                    {"at": row["at"], "ok": good, "note": (row["note"] or status)[:200]}})
            report.append(row)
        return {"ok": True, "resolvers": report, "load_errors": dict(hosters.LOAD_ERRORS)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "hoster_doctor failed: %s" % exc}


TOOLS = {"dl_add_hoster": tool_dl_add_hoster, "hoster_resolve": tool_hoster_resolve,
         "hoster_doctor": tool_hoster_doctor}

TEST_CHECKLIST = {
    "dl_add_hoster": {
        "does": "Queues links to file-hosting sites (MediaFire, Google Drive, Buzzheavier) with the hosters engine, which finds the real file link on the host's page and downloads it.",
        "steps": [
            {"run": {"url": "<a MediaFire file link>", "dry_run": True},
             "expect": "ok: true, dry_run: true, one decision with engine 'hosters' and a reason like 'MediaFire link (hosters engine)'; nothing is queued."},
            {"run": {"url": "<a MediaFire file link>", "start_paused": True},
             "expect": "ok: true, one paused row with engine hosters. Resume it: the log says which resolver found the link and the file lands in the download folder under its real name. ⚠️ Needs the real site."},
            {"run": {"url": "https://example.com/file.zip", "dry_run": True},
             "expect": "ok: false, the problem says the hosters engine can't take that link (no resolver handles that host); nothing is queued."},
            {"run": {"url": "<a MediaFire folder link: mediafire.com/folder/...>"},
             "expect": "ok: true and one row. When it runs it fails with a plain message that MediaFire folders aren't supported (nothing is guessed)."},
        ],
        "care": "Queues a real download (writes a file in the library root once resumed); use start_paused or dry_run to look without starting.",
        "watch": ["Owner only: from a Discord / Instagram guest it is refused and not even offered. dl_add takes the same links.",
                  "Cookies a host sets are never stored: only the page link is kept on the row."],
    },
    "hoster_resolve": {
        "does": "Says what the hub would do with a file-host link (which resolver, how it works, how reliable) and, by asking the host, the file name, size and any wait. Downloads nothing.",
        "steps": [
            {"run": {},
             "expect": "ok: true, a 'supported' list with buzzheavier, gdrive and mediafire, each with its hosts, method, confidence and last_check (null until hoster_doctor has run)."},
            {"run": {"url": "<a MediaFire file link>"},
             "expect": "ok: true, handled: true, resolver mediafire, and a probe with status ok, a file_host and (maybe) a filename. The file address is not shown in full. ⚠️ Needs the real site."},
            {"run": {"url": "<a MediaFire file link>", "probe": False},
             "expect": "ok: true, handled: true with the resolver's description and no probe (nothing contacted)."},
            {"run": {"url": "https://example.com/file.zip"},
             "expect": "ok: true, handled: false, would_use 'http': a plain link isn't a file-host link."},
        ],
        "care": "Contacts the file host from the PC (a few requests); downloads no file.",
        "watch": ["Owner only: a chat guest is refused."],
    },
    "hoster_doctor": {
        "does": "Checks that each file-host resolver still works by resolving a known-good test link the owner chose, and records what it saw.",
        "steps": [
            {"run": {},
             "expect": "ok: true, one row per resolver. With no test link set a row says no_canary and how to set one; nothing is contacted for it."},
            {"run": {"host": "mediafire"},
             "expect": "ok: true, one row for mediafire. With hosters.canary set (dl_settings) it shows status ok / dead / page_not_understood and the time it was checked. ⚠️ Needs the real site and a real test link."},
            {"run": {"host": "nope"},
             "expect": "ok: false, the error names the known hosts."},
        ],
        "care": "Contacts each file host once per canary link; downloads no file.",
        "watch": ["Owner only: a chat guest is refused.",
                  "A host that answers 'wait' or 'quota' counts as working (the page was understood)."],
    },
}
