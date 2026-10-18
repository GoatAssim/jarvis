"""What the tools, the panel and a future CLI call (dicts in, dicts out, never raises).

Tool handlers in actions/download_hub.py are thin wrappers over this module, so the rules
live once: what may be queued, what a pause means for each state, which actions a row
offers, and what is never opened or deleted.

A tool call is a short-lived process, so nothing here runs a transfer.  It enqueues rows,
records control requests, and reads state; the daemon does the work (daemon.py).

Control requests (pause / cancel):
  * the row is not held by a worker (queued, paused, stalled, failed ...), or the daemon is
    not running -> the change is applied directly;
  * a worker holds it and the daemon is alive -> `items.control` is set and the daemon's
    next tick (<= ~1 s) stops the worker and performs the change.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import engines, naming, netguard, notify, router, settings as settings_mod, states
from .store import Store

SOURCE_RE_PREFIXES = ("http://", "https://")


def _store(store):
    return store or Store()


def _err(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


# --------------------------------------------------------------------------- daemon / disk

def daemon_view(store):
    info = store.daemon_info()
    return {"alive": bool(info["alive"]), "pid": info["pid"],
            "age_s": None if info["age_s"] is None else round(info["age_s"], 1)}


def _free_bytes(path):
    """Free space where `path` will live (the nearest folder that exists)."""
    probe = Path(path)
    for _ in range(12):
        if probe.exists():
            try:
                return shutil.disk_usage(str(probe)).free
            except OSError:
                return None
        if probe.parent == probe:
            break
        probe = probe.parent
    return None


# --------------------------------------------------------------------------- adding

def _initial_title(source):
    name = naming.filename_from_url(source)
    if name:
        return naming.sanitize_filename(name, default="download")
    try:
        from urllib.parse import urlsplit
        return urlsplit(source).hostname or "download"
    except ValueError:
        return "download"


def preview(text, engine=None, store=None):
    """Router decision for one input without queueing anything (the panel's add box)."""
    store = _store(store)
    settings = store.get_settings()
    decision = router.route(text, engine=engine, settings=settings)
    if decision["ok"] and decision["engine"] == "http":
        try:
            netguard.check_url(decision["source"], settings["allow_private_hosts"])
        except netguard.GuardError as exc:
            decision = dict(decision, ok=False, available=False, problem=str(exc))
    return decision


ITEM_OPEN_VALUES = ("default",) + settings_mod.OPEN_MODES
ITEM_NOTIFY_VALUES = ("default", "on", "off")


def normalize_item_options(limit_kbps=None, open_when_done=None, notify_me=None):
    """The per-download extras -> ({options}, error).  A value of None leaves that option unset;
    "default" (or 0 for the speed) clears it so the global setting applies again."""
    out = {}
    if limit_kbps not in (None, ""):
        try:
            kbps = int(float(limit_kbps))
        except (TypeError, ValueError):
            return {}, "speed limit must be a number of KB/s (0 for no limit of its own)"
        if kbps < 0 or kbps > 10_000_000:
            return {}, "speed limit must be between 0 and 10,000,000 KB/s"
        out["limit_kbps"] = kbps or None
    if open_when_done not in (None, ""):
        text = str(open_when_done).strip().lower()
        text = "off" if text in ("none", "nothing", "no") else text
        if text not in ITEM_OPEN_VALUES:
            return {}, "open_when_done must be one of: %s" % ", ".join(ITEM_OPEN_VALUES)
        out["open"] = None if text == "default" else text
    if notify_me not in (None, ""):
        text = str(notify_me).strip().lower()
        text = {"true": "on", "yes": "on", "always": "on", "false": "off", "no": "off", "never": "off"}.get(text, text)
        if text not in ITEM_NOTIFY_VALUES:
            return {}, "notify must be one of: %s" % ", ".join(ITEM_NOTIFY_VALUES)
        out["notify"] = None if text == "default" else text
    return out, None


def add_items(inputs, *, engine=None, dest=None, group=None, start_paused=False,
              speed_limit_kbps=None, open_when_done=None, notify_me=None, store=None):
    """Queue one or more downloads.  Returns {ok, added, skipped, group, note}."""
    try:
        store = _store(store)
        if isinstance(inputs, str):
            inputs = [inputs]
        wanted = []
        for raw in inputs or []:
            text = str(raw).strip()
            if text and text not in wanted:
                wanted.append(text)
        if not wanted:
            return _err("give me a link to download")
        settings = store.get_settings()
        cap = int(settings["limits"]["max_items_per_call"])
        if len(wanted) > cap:
            return _err("that is %d links; the limit is %d per call" % (len(wanted), cap))

        root = settings_mod.library_root(settings)
        subdir = None
        if dest not in (None, ""):
            if naming.safe_subdir(root, dest) is None:
                return _err("dest must be a sub-folder of the library root (%s), not a full path or '..'. "
                            "Change the library root with dl_settings if you want files elsewhere." % root)
            subdir = str(dest).strip()

        extras, problem = normalize_item_options(speed_limit_kbps, open_when_done, notify_me)
        if problem:
            return _err(problem)
        extras = {k: v for k, v in extras.items() if v is not None}

        plan, skipped = [], []
        for text in wanted:
            decision = preview(text, engine=engine, store=store)
            if decision["ok"]:
                plan.append((text, decision))
            else:
                skipped.append({"input": states.clean_text(text, 120), "problem": decision["problem"]})
        added = []
        group_id = None
        if plan and (group or len(plan) > 1):
            group_id = store.ensure_group(group or "Batch of %d" % len(plan), kind="batch")
        for text, decision in plan:
            options = {"subdir": subdir} if subdir else {}
            options.update(extras)            # extras never reach the dedupe key (options_hash)
            title = _initial_title(decision["source"]) if decision["source"].lower().startswith(SOURCE_RE_PREFIXES) \
                else states.clean_text(text, 80)
            item, created = store.add_item(
                engine=decision["engine"], source_url=decision["source"], title=title,
                kind=naming.kind_for(title) if decision["kind"] == "file" else decision["kind"],
                options=options, group_id=group_id, state="paused" if start_paused else "queued",
                dedupe_key=router.dedupe_key(decision["engine"], router.canonical_url(decision["source"]), options))
            added.append({"id": item["id"], "title": item["title"], "engine": item["engine"],
                          "reason": decision["reason"], "state": item["state"], "duplicate": not created})
        out = {"ok": bool(added), "added": added, "skipped": skipped, "group": group_id,
               "count": len(added), "library_root": str(root)}
        if not added:
            out["error"] = skipped[0]["problem"] if skipped else "nothing was queued"
        if added and not daemon_view(store)["alive"]:
            out["note"] = ("The download hub daemon isn't running, so nothing will start until it is "
                           "(Menu > Daemons > Download hub).")
        return out
    except Exception as exc:  # noqa: BLE001 - tools never raise
        return _err("couldn't queue that: %s" % exc)


# --------------------------------------------------------------------------- reading

def _provider_map(store):
    return {p["id"]: p for p in store.list_providers()}


def effective_capabilities(item, provider=None, library_root=None):
    """What the panel may offer for a row (a button exists only if its capability does)."""
    state = item["state"]
    if item["owner"] == "hub":
        engine = engines.get(item["engine"])
        can_pause = engine is None or getattr(engine, "supports_pause", True)
        caps = []
        if state in states.ACTIVE_STATES or state == "queued":
            caps = (["pause"] if can_pause else []) + ["cancel"]
        elif state == "paused":
            caps = ["resume", "cancel"]
        elif state == "stalled":
            caps = ["retry"] + (["pause"] if can_pause else []) + ["cancel"]
        elif state in ("failed", "cancelled"):
            caps = ["retry"]
        elif state == "done" and item.get("dest_file"):
            caps = ["open", "reveal"]
        return caps
    declared = set((provider or {}).get("capabilities") or [])
    if item.get("capabilities") is not None:
        declared &= set(item["capabilities"])          # a row may narrow what its provider declared
    # A verb is only offered where it makes sense for the row's state, whatever the provider declared.
    sensible = set()
    if state in states.ACTIVE_STATES or state in ("queued", "stalled"):
        sensible |= {"pause"}
    if state == "paused":
        sensible |= {"resume"}
    if state not in states.FINAL_STATES:
        sensible |= {"cancel", "prioritize"}
    if state in ("failed", "cancelled", "stalled"):
        sensible |= {"retry"}
    if state in states.FINAL_STATES:
        sensible |= {"remove"}
    roots = (provider or {}).get("open_roots") or []
    dest = item.get("dest_file")
    if dest and roots and any(naming.under(r, dest) for r in roots):
        sensible |= {"open", "reveal", "play"}          # only for a file inside the provider's roots
    declared &= sensible
    return [c for c in states.CAPABILITIES if c in declared]


def present_item(item, providers, now):
    provider = providers.get(item["provider"]) if item["owner"] == "provider" else None
    last = item.get("last_report_at")
    return {
        "id": item["id"], "title": item["title"], "subtitle": item["subtitle"], "kind": item["kind"],
        "engine": item["engine"], "provider": item["provider"],
        "provider_label": item.get("provider_label") or (item["engine"] if item["owner"] == "hub" else item["provider"]),
        "provider_icon": item.get("provider_icon") or "", "owner": item["owner"],
        "state": item["state"], "phase": item["phase"], "phase_index": item["phase_index"],
        "phase_count": item["phase_count"], "percent": item["percent"], "bytes_done": item["bytes_done"],
        "bytes_total": item["bytes_total"], "speed_bps": item["speed_bps"], "eta_s": item["eta_s"],
        "derived": item["derived"], "peers": item["peers"], "seeds": item["seeds"],
        "group_id": item["group_id"], "group_title": item.get("group_title"),
        "dest": item.get("dest_file") or item.get("dest_dir"),
        "source": netguard.mask_url(item["source_url"]) if item.get("source_url") else "",
        "message": item["message"], "error": item["error"], "stale": item["stale"],
        "age_s": None if not last else max(0, int(now - last)),
        "attempts": item["attempts"], "created_at": item["created_at"], "finished_at": item["finished_at"],
        "retry_in_s": (max(0, int(item["next_attempt_at"] - now)) if item.get("next_attempt_at") else None),
        "capabilities": effective_capabilities(item, provider),
        "read_only": bool(item["owner"] == "provider" and not (provider or {}).get("capabilities")),
        "links": item.get("links") or [], "pending": item.get("control") or "",
        # this download's own choices (hub rows only): speed cap KB/s, when-done action, notify
        "prefs": ({k: (item.get("options") or {}).get(k) for k in ("limit_kbps", "open", "notify")
                   if (item.get("options") or {}).get(k) is not None} if item["owner"] == "hub" else {}),
    }


_STATE_GROUPS = {"active": states.ACTIVE_STATES, "final": states.FINAL_STATES,
                 "waiting": ("queued", "stalled", "paused")}


def list_items(*, state=None, engine=None, provider=None, kind=None, group=None, search=None,
               limit=50, store=None, with_summary=False):
    try:
        store = _store(store)
        wanted = None
        if state:
            parts = [p.strip() for p in str(state).split(",") if p.strip()]
            wanted = []
            for p in parts:
                if p in _STATE_GROUPS:
                    wanted += list(_STATE_GROUPS[p])
                elif p in states.STATES:
                    wanted.append(p)
                else:
                    return _err("unknown state %r (use one of: %s, active, final, waiting)"
                                % (p[:20], ", ".join(states.STATES)))
        now = store.now()
        rows = store.list_items(states_in=wanted, engine=engine, provider=provider, kind=kind,
                                group_id=group, search=search, limit=limit)
        providers = _provider_map(store)
        items = [present_item(r, providers, now) for r in rows]
        out = {"ok": True, "items": items, "count": len(items)}
        if with_summary:
            out["summary"] = summary(store)
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the downloads: %s" % exc)


def summary(store, full=False):
    settings = store.get_settings()
    root = settings_mod.library_root(settings)
    kbps, source = settings_mod.effective_rate_limit_kbps(settings)
    out = {"counts": store.counts(), "daemon": daemon_view(store), "library_root": str(root),
           "free_bytes": _free_bytes(root),
           # the speed limit in force right now (the owner's, or a schedule window's); the panel
           # shows it without needing the whole settings object on every poll
           "speed_limit": {"kbps": kbps, "source": source},
           "after_download": settings["after_download"]["open"],
           "notifications_on": bool(settings["notify"]["enabled"])}
    if full:
        out["engines"] = engines.availability()
        out["engine_load_errors"] = dict(engines.LOAD_ERRORS)
        out["settings"] = settings_view(settings)
    return out


def item_detail(item_id, store=None):
    """One download in full: the list row plus its detail pairs, sub-files and recent events
    (the panel's detail pane, and `dl_status(id=...)` for the model)."""
    try:
        store = _store(store)
        item = store.get_item(str(item_id or ""))
        if not item:
            return _err("no such download")
        row = present_item(item, _provider_map(store), store.now())
        row["detail"] = item.get("detail") or []
        row["children"] = item.get("children") or []
        row["events"] = store.list_events(item["id"], limit=30)
        row["control_pending"] = item.get("control") or ""
        return {"ok": True, "item": row}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read that download: %s" % exc)


def status(store=None, item_id=None):
    if item_id:
        return item_detail(item_id, store)
    try:
        store = _store(store)
        out = summary(store, full=True)
        out["ok"] = True
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the hub status: %s" % exc)


# --------------------------------------------------------------------------- settings

def library_info(settings):
    """The download folder as the panel shows it: path, whether it exists / is writable, free space."""
    root = settings_mod.library_root(settings)
    exists = root.is_dir()
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return {"path": str(root), "exists": exists, "writable": os.access(str(probe), os.W_OK),
            "free_bytes": _free_bytes(probe), "folders": {k: naming.kind_folder(k, settings.get("folders"))
                                                          for k in settings_mod.FOLDER_KINDS}}


def settings_view(settings):
    kbps, source = settings_mod.effective_rate_limit_kbps(settings)
    return {"library_root": str(settings_mod.library_root(settings)),
            "concurrency": settings["concurrency"], "rate_limit_kbps": settings["rate_limit_kbps"],
            "allow_private_hosts": settings["allow_private_hosts"], "http": settings["http"],
            "limits": settings["limits"],
            "schedule": settings.get("schedule") or [],
            "rate_limit_now": {"kbps": kbps, "source": source},
            "folders": settings["folders"], "after_download": settings["after_download"],
            "notify": settings["notify"], "library": library_info(settings)}


def get_settings(store=None):
    try:
        store = _store(store)
        return {"ok": True, "settings": settings_view(store.get_settings())}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the settings: %s" % exc)


def update_settings(updates, store=None):
    try:
        store = _store(store)
        clean, errors = settings_mod.validate_update(updates)
        if errors:
            return _err("; ".join(errors), errors=errors)
        changed = store.set_settings(clean)
        return {"ok": True, "changed": changed, "settings": settings_view(store.get_settings())}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't change the settings: %s" % exc)


def update_prefs(updates, *, id=None, store=None):
    """The everyday preferences, without the "are you sure" that `dl_settings` carries.

    No `id`: global preferences (speed limit, schedule, folders, notifications, open-when-done,
    connections).  The two keys that decide where files are written and which private hosts the
    hub may fetch from are refused here; they need `dl_settings`.
    With `id`: that one download's own speed limit / open-when-done / notify choice.
    """
    try:
        store = _store(store)
        if id:
            item = store.get_item(str(id))
            if not item:
                return _err("no such download")
            if item["owner"] != "hub":
                return _err("that row belongs to %s; its own tool decides how it runs" % (item["provider"] or "another tool"))
            if item["state"] in states.FINAL_STATES:
                return _err("that download has already finished")
            mapping = {"limit_kbps": "limit_kbps", "speed_limit_kbps": "limit_kbps",
                       "open": "open", "open_when_done": "open", "notify": "notify"}
            unknown = [k for k in (updates or {}) if k not in mapping]
            if unknown or not updates:
                return _err("for one download you can set limit_kbps, open and notify")
            raw = {mapping[k]: v for k, v in updates.items()}
            extras, problem = normalize_item_options(raw.get("limit_kbps"), raw.get("open"), raw.get("notify"))
            if problem:
                return _err(problem)
            options = dict(item.get("options") or {})
            for key, value in extras.items():
                if value is None:
                    options.pop(key, None)
                else:
                    options[key] = value
            fresh = store.update_item(item["id"], {"options": options}, touch=False)
            return {"ok": True, "id": item["id"], "options": (fresh or item).get("options") or options}
        clean, errors = settings_mod.validate_update(updates, allowed=settings_mod.PREFS_WRITABLE)
        if errors:
            return _err("; ".join(errors), errors=errors)
        changed = store.set_settings(clean)
        return {"ok": True, "changed": changed, "settings": settings_view(store.get_settings())}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't change the preferences: %s" % exc)


def send_test_notification(event="done", *, store=None, deliver=None):
    """A sample of one notification type through the real notifier (the panel's "Send test")."""
    try:
        store = _store(store)
        if event not in notify.EVENT_LABELS:
            return _err("event must be one of: %s" % ", ".join(notify.EVENT_LABELS))
        record = notify.Dispatcher(store, deliver=deliver).send_test(event, store.get_settings())
        delivered = list(record.get("delivered") or [])
        return {"ok": True, "event": event, "delivered": delivered,
                "failed_channels": list(record.get("failed_channels") or []),
                "note": ("Sent through the notifier: %s." % (", ".join(delivered) or "inbox only"))}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't send the test: %s" % exc)


# --------------------------------------------------------------------------- controls

def select(store, *, id=None, group=None, engine=None, provider=None, all=False):
    """The rows a control call applies to; at least one selector is required."""
    if id:
        item = store.get_item(str(id))
        return [item] if item else []
    if not (group or engine or provider or all):
        return None
    return store.list_items(group_id=group, engine=engine, provider=provider, limit=2000, include_disabled=True)


def _discard_part(store, item):
    """Delete the `.part` of a cancelled hub download -- only that file, only inside the
    library root.  The finished file is never touched."""
    ref = item.get("source_ref") or {}
    part = ref.get("part")
    if not part:
        return
    root = settings_mod.library_root(store.get_settings())
    if naming.under(root, part) and str(part).endswith(".part"):
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass


def control_provider_row(item, verb, provider):
    """Phase 5 plugs in here: run the provider's own control tool for `verb`.  Until then a
    provider row can't be controlled, and the answer says why."""
    caps = effective_capabilities(item, provider)
    if verb not in caps:
        return "read-only: %s doesn't offer %s" % ((provider or {}).get("label") or item["provider"], verb)
    return "controls for tool-supplied downloads arrive with phase 5"


def _apply(store, verb, item, daemon_alive, providers):
    """One item, one verb.  Returns ("changed" | "pending" | "skipped", detail)."""
    state = item["state"]
    if item["owner"] != "hub":
        return "skipped", control_provider_row(item, verb, providers.get(item["provider"]))
    engine = engines.get(item["engine"])
    held = state in states.ACTIVE_STATES and daemon_alive

    if verb == "pause":
        if engine is not None and not getattr(engine, "supports_pause", True):
            return "skipped", "the %s engine can't pause" % item["engine"]
        if state in states.FINAL_STATES:
            return "skipped", "already %s" % state
        if state == "paused":
            return "skipped", "already paused"
        if held:
            store.set_control(item["id"], "pause")
            return "pending", "pausing"
        ok = store.transition(item["id"], "paused", expect=state, message="paused")
        return ("changed", "paused") if ok else ("skipped", "its state just changed")

    if verb == "resume":
        if state != "paused":
            return "skipped", "not paused"
        ok = store.transition(item["id"], "queued", expect="paused", next_attempt_at=None, message=None)
        return ("changed", "queued") if ok else ("skipped", "its state just changed")

    if verb == "cancel":
        if state in states.FINAL_STATES:
            return "skipped", "already %s" % state
        if held:
            store.set_control(item["id"], "cancel")
            return "pending", "cancelling"
        ok = store.transition(item["id"], "cancelled", expect=state, message="cancelled")
        if ok:
            _discard_part(store, item)
            return "changed", "cancelled"
        return "skipped", "its state just changed"

    if verb == "retry":
        if state not in ("failed", "cancelled", "stalled"):
            return "skipped", "nothing to retry (state: %s)" % state
        ok = store.transition(item["id"], "queued", expect=state, error=None, message=None,
                              attempts=0, next_attempt_at=None, finished_at=None)
        return ("changed", "queued again") if ok else ("skipped", "its state just changed")
    return "skipped", "unknown action"


def control(verb, *, id=None, group=None, engine=None, provider=None, all=False, store=None):
    """pause / resume / cancel / retry on one item, a group, an engine, a provider or all."""
    try:
        store = _store(store)
        items = select(store, id=id, group=group, engine=engine, provider=provider, all=all)
        if items is None:
            return _err("say which download: an id, a group, an engine, a provider, or all=true")
        if not items:
            return _err("no such download")
        alive = daemon_view(store)["alive"]
        providers = _provider_map(store)
        out = {"ok": True, "changed": [], "pending": [], "skipped": []}
        for item in items:
            result, detail = _apply(store, verb, item, alive, providers)
            if result == "changed":
                out["changed"].append(item["id"])
            elif result == "pending":
                out["pending"].append(item["id"])
            else:
                out["skipped"].append({"id": item["id"], "reason": detail})
        if id and not (out["changed"] or out["pending"]):
            return _err(out["skipped"][0]["reason"] if out["skipped"] else "nothing changed", **out)
        # A "skipped" row is the normal answer for a bulk call (everything already paused);
        # only a single named download reports it as an error.
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("%s failed: %s" % (verb, exc))


def apply_control_directly(store, item):
    """The daemon, for a control request nobody holds (the worker ended meanwhile)."""
    verb = item.get("control")
    state = item["state"]
    if verb == "pause" and state not in states.FINAL_STATES:
        store.transition(item["id"], "paused", expect=state, message="paused")
    elif verb == "cancel" and state not in states.FINAL_STATES:
        if store.transition(item["id"], "cancelled", expect=state, message="cancelled"):
            _discard_part(store, item)
    store.set_control(item["id"], "")


# --------------------------------------------------------------------------- opening files

def _default_launch(path, action):
    """Open a file with its default program, or show it in the file manager.  Never raises."""
    try:
        path = str(path)
        if sys.platform.startswith("win"):
            if action == "reveal":
                subprocess.Popen(["explorer", "/select," + path])
            else:
                os.startfile(path)  # noqa: S606 - the path was checked against the library root
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if action == "reveal" else ["open", path])
        else:
            target = os.path.dirname(path) if action == "reveal" else path
            subprocess.Popen(["xdg-open", target])
        return True
    except Exception:  # noqa: BLE001
        return False


LAUNCH = _default_launch       # swapped by tests


def open_item(item_id, action="open", store=None):
    """Open a finished file or reveal it in the file manager.

    The path is read from the database, never from the caller, and must sit inside the
    library root (hub rows) or one of the provider's `open_roots`.
    """
    try:
        store = _store(store)
        if action not in ("open", "reveal", "folder"):
            return _err("action must be open, reveal or folder")
        item = store.get_item(str(item_id or ""))
        if not item:
            return _err("no such download")
        path = item.get("dest_file")
        if not path:
            return _err("that download has no file yet")
        if item["owner"] == "hub":
            roots = [settings_mod.library_root(store.get_settings())]
        else:
            roots = [Path(r) for r in ((store.get_provider(item["provider"]) or {}).get("open_roots") or [])]
        if not roots or not any(naming.under(r, path) for r in roots):
            return _err("that file isn't inside a folder the hub may open")
        if not os.path.exists(path):
            return _err("the file is no longer there: %s" % path)
        target = os.path.dirname(str(path)) if action == "folder" else path
        if not LAUNCH(target, "open" if action == "folder" else action):
            return _err("couldn't open it on this system")
        return {"ok": True, "action": action, "path": str(target)}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't open that: %s" % exc)


def open_library(store=None):
    """Open the download folder itself.  The path is the library root from the settings."""
    try:
        store = _store(store)
        root = settings_mod.library_root(store.get_settings())
        root.mkdir(parents=True, exist_ok=True)
        if not LAUNCH(root, "open"):
            return _err("couldn't open the folder on this system")
        return {"ok": True, "action": "folder", "path": str(root)}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't open the download folder: %s" % exc)


# Never opened by "open when done": opening these RUNS them (or runs a script inside them).
# They are shown in their folder instead.  A manual Open click is the owner's own decision and
# is not restricted here.
RISKY_EXTENSIONS = frozenset((
    ".exe", ".msi", ".msp", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl", ".msc", ".dll", ".sys",
    ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".jar", ".lnk", ".url",
    ".reg", ".inf", ".scf", ".gadget", ".application", ".appx", ".msix", ".appxbundle",
    ".sh", ".command", ".app", ".apk", ".appimage", ".py", ".pyw", ".pl", ".rb",
    ".docm", ".xlsm", ".pptm", ".xlam", ".dotm", ".html", ".htm", ".xhtml", ".mht", ".svg",
))
AUTO_OPEN_MAX = 3            # at most this many automatic opens ...
AUTO_OPEN_WINDOW_S = 60      # ... per this many seconds, so a 30-file batch can't open 30 windows


def is_risky_to_open(path):
    return os.path.splitext(str(path))[1].lower() in RISKY_EXTENSIONS


def run_auto_open(store, settings, since, recent, now=None):
    """Do the "when it finishes" action for downloads that just finished.  Once per finish.

    `recent`: a deque of the times of recent automatic opens (the daemon keeps one).  Hub rows
    only; the file must be inside the library root (open_item enforces that again).  Returns the
    list of (item id, what happened) for the tests and the daemon's log.
    """
    now = time.time() if now is None else now
    out = []
    default_mode = (settings.get("after_download") or {}).get("open", "off")
    for item in store.unclaimed("open", "done", since=since, stamp_column="finished_at"):
        if item["owner"] != "hub":
            continue
        if not store.claim_notice(item["id"], "open", item.get("finished_at") or 0):
            continue
        mode = (item.get("options") or {}).get("open")
        mode = mode if mode in settings_mod.OPEN_MODES else default_mode
        if mode == "off" or not item.get("dest_file"):
            continue
        while recent and now - recent[0] > AUTO_OPEN_WINDOW_S:
            recent.popleft()
        if len(recent) >= AUTO_OPEN_MAX:
            store.add_event(item["id"], "info", "not opened automatically: too many downloads finished at once")
            out.append((item["id"], "skipped"))
            continue
        note = {"open": "opened automatically", "reveal": "shown in its folder automatically",
                "folder": "its folder was opened automatically"}[mode]
        if mode == "open" and is_risky_to_open(item["dest_file"]):
            mode, note = "reveal", "shown in its folder instead of opened: programs and scripts are never opened automatically"
        result = open_item(item["id"], mode, store=store)
        recent.append(now)
        if result.get("ok"):
            store.add_event(item["id"], "info", note)
            out.append((item["id"], mode))
        else:
            store.add_event(item["id"], "warn", "couldn't open it automatically: %s" % result.get("error"))
            out.append((item["id"], "failed"))
    return out
