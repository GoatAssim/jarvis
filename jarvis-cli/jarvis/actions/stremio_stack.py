"""Auto-discovered tool file: the Stremio stack (Download Hub plan, phase 4).

Brings Stremio's streaming server (and, if wanted, stremio-web) up and down, says whether it is
healthy, prepares it for downloading, and looks after its cache.  The torrent engine that uses
the server is `hub/engines/stremio.py`; magnets and Stremio streams go in through `dl_add`.

  stack_status / stack_start / stack_stop / stack_doctor   is it up, bring it up, take it down,
                                       what is wrong.  Detection is by ANSWERING on the port, not
                                       by daemon state: a server the Stremio app already opened
                                       reads "already up" and no second one is started.  Stremio's
                                       files are found without any setup (hub/stremio/detect.py:
                                       a running Stremio, Windows' install list, the Start Menu,
                                       the usual folders), and every outcome is its own plain
                                       sentence (hub/stremio/status.py).  The paths are saved only
                                       by the owner's Locate... button (hub_admin), never a tool
  stremio_prepare                      cache unlimited (a finite cache can evict a finished
                                       episode), cache folder, connection and speed limits
                                       (confirm-gated: it changes the server's own settings)
  server_cache / server_cache_clear    what the server has cached; clear it (confirm-gated:
                                       stops the server, empties its cache folder, restarts it)
  DAEMONS                              `server` and `web` (Menu > Daemons); both run through
                                       small runner scripts that read ~/.jarvis/stremio.json when
                                       they LAUNCH, so a corrected path needs no re-registration

Never import ai_client / tool_router / tool_registry here at module level (circular import: the
tool would silently vanish).  The hub package is imported inside the handlers.
"""

import os
import shutil
import time
from pathlib import Path

GB = 1024 ** 3
START_WAIT_S = 25                 # stremio-web only (it builds on first start); the server's wait is a setting
DEFAULT_START_WAIT_S = 60         # hub setting stremio.start_wait_seconds (10 to 300)
STOP_WAIT_S = 15
SKIP_DEFAULT_ROOT_NAMES = ("", ".", "/")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fail(label, exc):
    return {"ok": False, "error": "%s failed: %s" % (label, exc)}


def _mods():
    from ..hub.stremio import config, server
    return config, server


def _gb(n):
    return "%.1f GB" % (float(n) / GB)


def _free(path):
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


def _hub_items():
    """The hub's Stremio rows that are running right now (a stop would interrupt them)."""
    try:
        from ..hub import states
        from ..hub.store import Store
        return Store().list_items(engine="stremio", states_in=states.ACTIVE_STATES, limit=200)
    except Exception:  # noqa: BLE001 - the hub store being unreadable must not block a status call
        return []


def _ensure_daemon(did):
    """(True, "") when the daemon is registered, syncing the tool declarations once if it isn't yet
    (so the FIRST start works without restarting Jarvis).  (False, why) in plain words otherwise."""
    from .. import daemons
    if daemons.get(did):
        return True, ""
    why = ""
    try:
        from .. import tool_daemons
        result = tool_daemons.sync_if_stale(force=True)
        if result is None:
            try:
                from .. import atomic_io
                recorded = atomic_io.read_json(tool_daemons._state_path(), default={}, expect=dict).get("error")  # noqa: SLF001
            except Exception:  # noqa: BLE001
                recorded = None
            why = "registering the tool's daemons failed: %s" % str(recorded or "no reason recorded")[:160]
        else:
            for conflict in result.get("conflicts") or []:
                if did in str(conflict):
                    why = str(conflict)[:200]
    except Exception as exc:  # noqa: BLE001
        why = "registering the tool's daemons failed: %s: %s" % (type(exc).__name__, str(exc)[:120])
    if daemons.get(did):
        return True, ""
    return False, why or "the daemon list still doesn't have it after registering (is the Stremio tool switched off in Menu > Tool Manager?)"


def _start_wait():
    """How long stack_start waits for the server to answer: hub setting stremio.start_wait_seconds."""
    try:
        from ..hub.store import Store
        value = (Store().get_settings().get("stremio") or {}).get("start_wait_seconds")
        return max(10, min(300, int(value)))
    except Exception:  # noqa: BLE001 - settings unreadable: the default
        return DEFAULT_START_WAIT_S


def _wait_for(check, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(1.0)
    return bool(check())


def _server_view(config, server, wait=None):
    """The server block of stack_status: the old keys (url, up, version, daemon, managed_by_jarvis,
    note, problem, can_start, hint) plus the plain-words state (see hub/stremio/status.py)."""
    from ..hub.stremio import status
    snap = status.snapshot(wait_seconds=wait or _start_wait())
    daemon = server.daemon_state(server.SERVER_DAEMON)
    up = snap["state"] == "running"
    out = {"url": snap["address"], "up": up, "version": snap["version"] or None, "daemon": daemon["status"],
           "managed_by_jarvis": bool(up and daemon["running"]), "state": snap["state"], "title": snap["title"],
           "sentence": snap["sentence"], "next": snap["next"], "step": snap["step"], "can_start": snap["can_start"],
           "path": snap["path"]}
    if snap["problems"]:
        out["problems"] = snap["problems"]
    if up and not daemon["running"]:
        out["note"] = "already up (started outside Jarvis, e.g. by the Stremio app); I won't start a duplicate"
    if not up:
        out["problem"] = snap["sentence"]
        if not snap["can_start"]:
            out["hint"] = snap["sentence"]
    return out


def _web_view(config, server):
    url = config.web_url()
    answering = server.port_answers(url)
    daemon = server.daemon_state(server.WEB_DAEMON)
    out = {"url": url, "up": answering, "daemon": daemon["status"], "configured": bool(config.web_dir())}
    if answering and not daemon["running"]:
        out["note"] = "already up (not started by Jarvis)"
    return out


# ---------------------------------------------------------------------------
# stack_status / start / stop / doctor
# ---------------------------------------------------------------------------


def tool_stack_status(args):
    try:
        config, server = _mods()
        srv, web = _server_view(config, server), _web_view(config, server)
        hub = {"daemon_alive": False, "active": len(_hub_items())}
        try:
            from ..hub.store import Store
            hub["daemon_alive"] = bool(Store().daemon_info()["alive"])
        except Exception:  # noqa: BLE001
            pass
        bits = ["server: %s" % ("already up" if srv.get("note") else "up" if srv["up"] else "down (%s)" % srv["title"].lower())
                + (" (v%s)" % srv["version"] if srv["up"] else ""),
                "stremio-web: %s" % ("up" if web["up"] else "down"),
                "download hub: %s" % ("running" if hub["daemon_alive"] else "not running")]
        return {"ok": True, "server": srv, "web": web, "hub": hub, "summary": "; ".join(bits)}
    except Exception as exc:  # noqa: BLE001
        return _fail("stack_status", exc)


def _await_server(srv, seconds, daemon_id):
    """Wait for the server to answer.  'up', 'exited' (the program stopped while we waited) or 'timeout'."""
    from .. import daemons
    begun = time.monotonic()
    end = begun + seconds
    while True:
        if srv.is_up(timeout=1.5)[0]:
            return "up"
        try:
            state = daemons.status(daemon_id)
        except Exception:  # noqa: BLE001
            state = {}
        label = state.get("status")
        if label in ("crashed", "restarting") or (label == "stopped" and time.monotonic() - begun >= 3):
            return "exited"
        if time.monotonic() >= end:
            return "timeout"
        time.sleep(1.0)


def _start_server(config, server):
    """Bring Stremio's server up, or say in one plain sentence why not.  The result keeps the old
    `state` words (already_up / started / blocked / error) and adds `outcome` (the fine-grained id from
    hub/stremio/status.py, or not_registered / disabled / launch_refused) and `sentence`."""
    from ..hub.stremio import status
    wait = _start_wait()
    snap = status.snapshot(wait_seconds=wait)
    base = {"url": snap["address"], "outcome": snap["state"], "sentence": snap["sentence"]}
    if snap["problems"]:
        base["problems"] = snap["problems"]
    if snap["state"] == "running":
        outside = snap["managed_by"] == "outside"
        return {**base, "state": "already_up", "version": snap["version"],
                "note": "already up" + (" (started outside Jarvis)" if outside else "") + "; nothing was started"}
    if snap["state"] in ("other_port", "port_busy", "app_open", "files_missing", "not_found"):
        return {**base, "state": "blocked" if snap["state"] == "port_busy" else "error", "error": snap["sentence"]}
    from .. import daemons
    if snap["state"] in ("ready", "start_failed"):
        registered, why = _ensure_daemon(server.SERVER_DAEMON)
        if not registered:
            return {**base, "state": "error", "outcome": "not_registered",
                    "error": "Jarvis couldn't set up the Stremio server task, so it can't start it (%s). Restarting Jarvis may fix it." % why}
        ok, message = daemons.start(server.SERVER_DAEMON)
        if not ok and "already" not in message:
            if "disabled" in message:
                return {**base, "state": "error", "outcome": "disabled",
                        "error": "The Stremio server task is switched off, so Jarvis can't start it. Turn it on in Menu > Daemons."}
            return {**base, "state": "error", "outcome": "launch_refused",
                    "error": "Jarvis couldn't start the Stremio program: %s" % str(message)[:200]}
    server.forget_probe()
    srv = server.StremioServer()
    result = _await_server(srv, wait, server.SERVER_DAEMON)
    if result == "up":
        version = srv.is_up()[1]
        return {**base, "state": "started", "outcome": "started", "version": version,
                "sentence": "Stremio's server is running at %s (version %s). Jarvis started it." % (snap["address"], version)}
    after = status.snapshot(wait_seconds=wait, waited=wait if result == "timeout" else None, exited=(result == "exited"))
    out = {**base, "state": "blocked" if after["state"] == "port_busy" else "error", "outcome": after["state"],
           "sentence": after["sentence"], "error": after["sentence"]}
    if after.get("next"):
        out["next"] = after["next"]
    return out


def _start_web(config, server):
    url = config.web_url()
    if server.port_answers(url):
        return {"state": "already_up", "url": url}
    if not config.is_this_pc(url):
        return {"state": "not_local", "url": url,
                "note": "stremio-web isn't answering at %s, and that address isn't on this PC, so Jarvis can't start it there" % url}
    if not config.web_dir():
        return {"state": "not_configured",
                "note": "stremio-web isn't set up (put its folder in web.dir of ~/.jarvis/stremio.json); playback links need it"}
    registered, why = _ensure_daemon(server.WEB_DAEMON)
    if not registered:
        return {"state": "error", "error": "Jarvis couldn't set up the stremio-web task, so it can't start it (%s). Restarting Jarvis may fix it." % why}
    from .. import daemons
    ok, message = daemons.start(server.WEB_DAEMON)
    if not ok and "already" not in message:
        return {"state": "error", "error": message}
    came_up = _wait_for(lambda: server.port_answers(url), START_WAIT_S)
    return {"state": "started" if came_up else "starting", "url": url,
            **({} if came_up else {"note": "stremio-web takes a while to build the first time; check stack_status shortly"})}


def tool_stack_start(args):
    try:
        what = str((args or {}).get("what") or "both").strip().lower()
        if what not in ("server", "web", "both"):
            return {"ok": False, "error": "what must be server, web or both"}
        config, server = _mods()
        out = {"ok": True}
        if what in ("server", "both"):
            out["server"] = _start_server(config, server)
            if out["server"]["state"] not in ("already_up", "started"):
                out["ok"] = False
                out["error"] = out["server"]["error"]
            if out["server"].get("problems"):
                out["config_problems"] = out["server"]["problems"]
        if what in ("web", "both") and out["ok"]:
            out["web"] = _start_web(config, server)
            if out["web"]["state"] == "error":
                out["ok"] = False
                out["error"] = out["web"]["error"]
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("stack_start", exc)


def _stop_daemon(did, server):
    from .. import daemons
    state = server.daemon_state(did)
    if not state["running"]:
        return {"state": "not_running_by_jarvis"}
    ok, message = daemons.stop(did)
    return {"state": "stopped" if ok else "error", "message": message}


def tool_stack_stop(args):
    try:
        what = str((args or {}).get("what") or "both").strip().lower()
        if what not in ("server", "web", "both"):
            return {"ok": False, "error": "what must be server, web or both"}
        config, server = _mods()
        out = {"ok": True}
        if what in ("server", "both"):
            srv = server.StremioServer()
            up, _info = srv.is_up()
            managed = server.daemon_state(server.SERVER_DAEMON)["running"]
            if up and not managed:
                out["server"] = {"state": "not_ours",
                                 "note": "the server is up but wasn't started by Jarvis (probably the Stremio app); "
                                         "close it there. I don't kill programs I didn't start"}
            else:
                busy = _hub_items()
                playing = []
                if up:
                    try:
                        playing = server.active_torrents(srv.all_stats(timeout=3))
                    except server.ServerError:
                        playing = []
                if (busy or playing) and up:
                    from .. import ui_bridge as ui
                    body = []
                    if busy:
                        body.append("%d download(s) in the hub are running and will retry when the server is back." % len(busy))
                    if playing:
                        body.append("Something is streaming from the server right now; it will stop.")
                    if not ui.confirm("Stop the Stremio server?", body="\n".join(body), level="warn",
                                      confirm_label="Stop it", cancel_label="Leave it running", default=False):
                        return {"ok": False, "cancelled": True, "error": "not confirmed: the server is still running"}
                out["server"] = _stop_daemon(server.SERVER_DAEMON, server)
                server.forget_probe()
        if what in ("web", "both"):
            out["web"] = _stop_daemon(server.WEB_DAEMON, server)
        errors = [v.get("message") for v in out.values() if isinstance(v, dict) and v.get("state") == "error"]
        if errors:
            out["ok"] = False
            out["error"] = "; ".join(str(e) for e in errors)
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("stack_stop", exc)


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def tool_stack_doctor(args):
    try:
        config, server = _mods()
        cfg = config.load()
        checks = []
        path = config.config_path()
        checks.append(_check("config file", "ok" if path.exists() else "ok",
                             str(path) if path.exists() else "none (defaults are used): %s" % path))
        for problem in config.problems(cfg):
            checks.append(_check("config", "warn", problem))
        from ..hub.stremio import detect, status
        snap = status.snapshot(wait_seconds=_start_wait())
        path = snap["path"]
        exe, js, where = path.get("runtime_exe"), path.get("server_js"), path.get("where")
        checks.append(_check("server files", "ok" if exe else "warn",
                             ("%s + %s (found %s)" % (exe, js, detect.HOW.get(where, where))) if exe else snap["sentence"]))
        for probe in snap["tried"]:
            checks.append(_check("looked: %s" % probe["probe"], "ok" if (probe["found"] or exe) else "warn", probe["detail"]))
        checks.append(_check("state", "ok" if snap["state"] in ("running", "ready", "starting") else "warn",
                             "%s: %s" % (snap["title"], snap["sentence"])))
        checks.append(_check("start wait", "ok", "stack_start waits up to %d seconds for the server (hub setting stremio.start_wait_seconds)"
                             % _start_wait()))
        srv = server.StremioServer(snap["address"])
        up, info = srv.is_up()
        if up:
            checks.append(_check("server", "ok", "answering at %s, version %s" % (srv.base, info)))
            try:
                values = srv.values(timeout=4)
                size = values.get("cacheSize")
                checks.append(_check("cache", "ok" if size is None else "warn",
                                     "folder %s; size %s%s" % (values.get("cacheRoot"), "unlimited" if size is None else _gb(size),
                                                               "" if size is None else " (finite: a finished episode can be evicted; stremio_prepare sets unlimited)")))
                checks.append(_check("limits", "ok", "connections %s, download soft/hard %s / %s bytes/s"
                                     % (values.get("btMaxConnections"), values.get("btDownloadSpeedSoftLimit"),
                                        values.get("btDownloadSpeedHardLimit"))))
                free = _free(values.get("cacheRoot") or "")
                if free is not None:
                    checks.append(_check("cache drive space", "ok" if free > 10 * GB else "warn", "%s free" % _gb(free)))
            except server.ServerError as exc:
                checks.append(_check("server settings", "warn", str(exc)))
        else:
            checks.append(_check("server", "fail", snap["sentence"] if snap["state"] != "ready"
                                 else "%s (stack_start brings it up)" % snap["sentence"]))
        web = config.web_url()
        checks.append(_check("stremio-web", "ok" if server.port_answers(web) else "warn",
                             ("answering at %s" % web) if server.port_answers(web)
                             else "not answering at %s (%s)" % (web, "stack_start starts it" if config.web_dir()
                                                              else "web.dir isn't set in ~/.jarvis/stremio.json; playback links need it")))
        pnpm = config.which("pnpm")
        if config.web_dir():
            checks.append(_check("pnpm", "ok" if pnpm else "warn", pnpm or "not on PATH (web.start_cmd needs it)"))
        from ..hub.stremio import player
        kind, pexe, problem = player.find()
        checks.append(_check("player", "ok" if kind else "warn", ("%s: %s" % (kind, pexe)) if kind else problem))
        for did, label in ((server.SERVER_DAEMON, "server daemon"), (server.WEB_DAEMON, "web daemon")):
            state = server.daemon_state(did)
            checks.append(_check(label, "ok", state["status"] if state["registered"] else "not registered yet"))
        try:
            from ..hub import engines
            avail = engines.availability().get("stremio")
            if avail is None:
                checks.append(_check("stremio engine", "fail", "not loaded: %s" % engines.LOAD_ERRORS.get("stremio", "unknown")))
            else:
                checks.append(_check("stremio engine", "ok" if avail["available"] else "warn",
                                     "ready" if avail["available"] else avail["reason"]))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("stremio engine", "warn", "couldn't check: %s" % exc))
        failed = [c for c in checks if c["status"] == "fail"]
        return {"ok": not failed, "checks": checks, "config": config.public_view(cfg), "state": snap["state"],
                "diagnostic": status.diagnostic_text(snap, ["%s [%s] %s" % (c["name"], c["status"], c["detail"]) for c in checks
                                                           if not c["name"].startswith("looked:")]),
                "summary": "%d check(s), %d failed, %d warning(s)" % (len(checks), len(failed),
                                                                      len([c for c in checks if c["status"] == "warn"]))}
    except Exception as exc:  # noqa: BLE001
        return _fail("stack_doctor", exc)


# ---------------------------------------------------------------------------
# stremio_prepare
# ---------------------------------------------------------------------------


def _clean_cache_root(text):
    """An absolute folder path, or (None, why).  Not a drive root, no '..'."""
    raw = str(text or "").strip().strip('"')
    if not raw or len(raw) > 240 or any(c in raw for c in "\r\n\x00"):
        return None, "cache_root must be a folder path"
    if not (os.path.isabs(raw) or (len(raw) > 2 and raw[1] == ":" and raw[2] in "\\/")):
        return None, "cache_root must be a full path like D:/StremioCache"
    if ".." in Path(raw.replace("\\", "/")).parts:
        return None, "cache_root can't contain '..'"
    if Path(raw).parent == Path(raw):
        return None, "cache_root must be a folder, not a whole drive"
    return raw, None


def _number(value, label, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % label)
    if not low <= number <= high:
        raise ValueError("%s must be between %s and %s" % (label, low, high))
    return number


def tool_stremio_prepare(args):
    try:
        args = args or {}
        config, server = _mods()
        changes, notes = {}, []
        cache = str(args.get("cache") if args.get("cache") is not None else "unlimited").strip().lower()
        want_gb = None
        if cache in ("unlimited", "infinite", "none", "off", ""):
            changes["cacheSize"] = None
        elif cache == "keep":
            pass
        else:
            want_gb = _number(cache.replace("gb", "").strip(), "cache", 1, 10000)
            changes["cacheSize"] = int(want_gb * GB)
        root = args.get("cache_root")
        if root:
            clean, problem = _clean_cache_root(root)
            if problem:
                return {"ok": False, "error": problem}
            changes["cacheRoot"] = clean
        if args.get("max_connections") is not None:
            changes["btMaxConnections"] = int(_number(args["max_connections"], "max_connections", 10, 500))
        if args.get("speed_limit_mbps") is not None:
            soft = int(_number(args["speed_limit_mbps"], "speed_limit_mbps", 0.5, 1000) * 1024 * 1024)
            changes["btDownloadSpeedSoftLimit"] = soft
            changes["btDownloadSpeedHardLimit"] = int(soft * 1.4)      # the server's own defaults are 2.5 / 3.5 MiB/s
        if not changes:
            return {"ok": True, "changed": {}, "note": "nothing to change"}
        srv = server.StremioServer()
        up, info = srv.is_up()
        if not up:
            return {"ok": False, "error": "%s. stremio_prepare changes the running server's settings; start it first (stack_start)" % info}

        # the disk check (plan 7.3: "optionally point cacheRoot at a drive with space")
        target_root = changes.get("cacheRoot") or srv.values().get("cacheRoot")
        free = _free(target_root) if target_root else None
        disk = {"cache_drive_free": _gb(free) if free is not None else "unknown"}
        if want_gb is not None and free is not None and free < want_gb * GB * 1.1:
            return {"ok": False, "error": "the cache folder's drive has %s free, not enough for a %s cache (plus 10%%)"
                                          % (_gb(free), _gb(want_gb * GB)), **disk}
        if free is not None and free < 10 * GB and changes.get("cacheSize", 0) is None:
            notes.append("only %s is free on the cache drive; an unlimited cache will fill it" % _gb(free))
        if changes.get("cacheRoot"):
            try:
                Path(changes["cacheRoot"]).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": "can't create the cache folder: %s" % exc, **disk}
            notes.append("the old cache stays where it was; new downloads use the new folder")
        result = srv.set_settings(changes)
        out = {"ok": result["ok"], "changed": result["changed"], **disk}
        if notes:
            out["notes"] = notes
        if not result["ok"]:
            out["error"] = ("the server didn't keep: %s. This server version may need a setting changed in the Stremio app itself"
                            % ", ".join(sorted(result["mismatch"])))
            out["server_says"] = result["mismatch"]
        return out
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _fail("stremio_prepare", exc)


# ---------------------------------------------------------------------------
# server_cache / server_cache_clear
# ---------------------------------------------------------------------------


def _folder_size(folder, max_files=20000, max_seconds=4.0):
    total = count = 0
    end = time.monotonic() + max_seconds
    truncated = False
    for base, dirs, files in os.walk(str(folder), followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
            count += 1
            if count >= max_files or time.monotonic() > end:
                return total, count, True
    return total, count, truncated


def _cache_folder(values):
    """The server's cache folder for torrents: <cacheRoot>/stremio-cache, or None."""
    root = values.get("cacheRoot")
    if not root:
        return None, None
    base = Path(str(root))
    return base, base / "stremio-cache"


def tool_server_cache(args):
    try:
        config, server = _mods()
        srv = server.StremioServer()
        up, info = srv.is_up()
        if not up:
            return {"ok": False, "error": "%s (start it with stack_start)" % info}
        values = srv.values()
        base, folder = _cache_folder(values)
        out = {"ok": True, "cache_root": str(base) if base else None,
               "cache_size": "unlimited" if values.get("cacheSize") is None else _gb(values["cacheSize"])}
        if folder is not None and folder.is_dir():
            size, count, cut = _folder_size(folder)
            out["on_disk"] = {"size": _gb(size), "files": count, "partial_count": cut}
        free = _free(base) if base else None
        if free is not None:
            out["drive_free"] = _gb(free)
        known = {}
        for item in _hub_items_all():
            from ..hub.stremio import links
            ident = links.parse_magnet(item.get("source_url") or "") or {}
            ref = item.get("source_ref") or {}
            digest = ident.get("infohash") or ref.get("infohash")
            if digest:
                known[digest] = {"id": item["id"], "state": item["state"]}
        torrents = []
        for digest, stat in sorted((srv.all_stats(timeout=6) or {}).items())[:50]:
            if not isinstance(stat, dict):
                continue
            files = stat.get("files") or []
            torrents.append({"infohash": str(digest)[:40], "name": str(stat.get("name") or "(no metadata: no peers yet)")[:100],
                             "files": len(files), "size": _gb(sum(int(f.get("length") or 0) for f in files)) if files else None,
                             "downloaded": _gb(stat.get("downloaded") or 0), "peers": stat.get("peers"),
                             "known_to_jarvis": known.get(str(digest))})
        out["torrents"] = torrents
        out["note"] = ("finished downloads in the hub are ordinary files under the library; this cache is Stremio's own "
                       "working copy and server_cache_clear empties it")
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("server_cache", exc)


def _hub_items_all():
    try:
        from ..hub.store import Store
        return Store().list_items(engine="stremio", limit=500)
    except Exception:  # noqa: BLE001
        return []


def _empty_folder(folder):
    """Delete what is INSIDE `folder` (never the folder, never through a link).  (files, bytes, errors)."""
    files = size = 0
    errors = []
    for entry in sorted(os.listdir(str(folder))):
        full = os.path.join(str(folder), entry)
        try:
            if os.path.islink(full):
                os.unlink(full)
                continue
            if os.path.isdir(full):
                for base, _dirs, names in os.walk(full, followlinks=False):
                    for name in names:
                        try:
                            size += os.lstat(os.path.join(base, name)).st_size
                            files += 1
                        except OSError:
                            pass
                shutil.rmtree(full)
            else:
                size += os.lstat(full).st_size
                files += 1
                os.remove(full)
        except OSError as exc:
            errors.append("%s: %s" % (entry[:60], exc.strerror or exc))
    return files, size, errors


def tool_server_cache_clear(args):
    """stop the server -> empty <cacheRoot>/stremio-cache -> start it again.  Confirm-gated."""
    try:
        config, server = _mods()
        srv = server.StremioServer()
        up, info = srv.is_up()
        if not up:
            return {"ok": False, "error": "I need the server running once to read its cache folder (stack_start); I won't guess a folder to delete"}
        values = srv.values()
        base, folder = _cache_folder(values)
        if folder is None:
            return {"ok": False, "error": "the server didn't report a cache folder"}
        try:
            if folder.is_symlink() or not folder.exists():
                return {"ok": True, "cleared": 0, "note": "there is no cache folder yet (%s); nothing to clear" % folder}
            if folder.name != "stremio-cache" or folder.resolve().parent != base.resolve():
                return {"ok": False, "error": "the cache folder isn't where I expect (<cacheRoot>/stremio-cache); I won't delete there"}
        except OSError as exc:
            return {"ok": False, "error": "can't read the cache folder: %s" % exc}
        busy = _hub_items()
        if busy:
            return {"ok": False, "error": "%d Stremio download(s) are running in the hub; pause or finish them first (dl_pause)" % len(busy)}
        try:
            if server.active_torrents(srv.all_stats(timeout=4)):
                return {"ok": False, "error": "something is downloading or streaming from the server right now; stop it first"}
        except server.ServerError:
            pass
        managed = server.daemon_state(server.SERVER_DAEMON)["running"]
        if not managed:
            return {"ok": False, "error": "the server wasn't started by Jarvis (probably the Stremio app). Close it there, "
                                          "then run this again and I'll clear the folder with nothing holding it open"}
        from .. import daemons
        ok, message = daemons.stop(server.SERVER_DAEMON)
        if not ok:
            return {"ok": False, "error": "couldn't stop the server: %s; nothing was deleted" % message}
        server.forget_probe()
        if not _wait_for(lambda: not srv.is_up(timeout=1.0)[0], STOP_WAIT_S):
            return {"ok": False, "error": "the server is still answering after stopping; nothing was deleted"}
        files, size, errors = _empty_folder(folder)
        started = _start_server(config, server)
        out = {"ok": not errors, "cleared_files": files, "freed": _gb(size), "server": started["state"]}
        if errors:
            out["error"] = "some files couldn't be deleted: %s" % "; ".join(errors[:3])
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("server_cache_clear", exc)


# ---------------------------------------------------------------------------
# schemas, routing, confirm gating
# ---------------------------------------------------------------------------

_WHAT = {"type": "string", "enum": ["server", "web", "both"],
         "description": "Which part: the streaming server (what downloads and streaming need), stremio-web (the screen playback links open), or both (default)."}

TOOL_SCHEMAS = [
    {"name": "stack_status",
     "description": ("Is the Stremio stack up? The streaming server (answers on its port, even if the Stremio app started it), "
                     "stremio-web, and the download hub's daemon, with versions and how many Stremio downloads are running. "
                     "When the server is down it says why in one plain sentence (not found, not running, app open but silent, other port, port in use)."),
     "parameters": {"type": "object", "properties": {}}},
    {"name": "stack_start",
     "description": ("Start the Stremio streaming server and/or stremio-web (Menu > Daemons). Finds the Stremio app by itself. "
                     "If the server already answers (the Stremio app is open) it says 'already up' and starts nothing. "
                     "If it can't, the error is one plain sentence: not found, could not start the program, started but silent, "
                     "answers on another port, port in use, or the app is open but its server isn't answering."),
     "parameters": {"type": "object", "properties": {"what": _WHAT}}},
    {"name": "stack_stop",
     "description": ("Stop the Stremio server and/or stremio-web that Jarvis started. Asks first when downloads or a stream are running. "
                     "A server started by the Stremio app itself is left alone."),
     "parameters": {"type": "object", "properties": {"what": _WHAT}}},
    {"name": "stack_doctor",
     "description": ("Diagnose the Stremio setup: what it looked at to find Stremio (running program, Windows' install list, Start Menu, "
                     "usual folders) and what each found, the state in plain words, server files, version, ports, cache folder and size, "
                     "free space, stremio-web, a video player, and whether the hub's Stremio engine is ready. Also returns a copyable `diagnostic` text."),
     "parameters": {"type": "object", "properties": {}}},
    {"name": "stremio_prepare",
     "description": ("Get the Stremio server ready for downloading (asks the person first). Default: set its cache to unlimited so "
                     "a finished episode can't be evicted. Optionally move the cache folder, raise the connection count or set a "
                     "download speed limit. Checks free disk space first."),
     "parameters": {"type": "object", "properties": {
         "cache": {"type": "string", "description": "'unlimited' (default), 'keep' to leave it, or a size in GB like '10'."},
         "cache_root": {"type": "string", "description": "A full folder path for the cache, e.g. 'D:/StremioCache'."},
         "max_connections": {"type": "integer", "description": "Peer connections the server may open (10-500; default 55)."},
         "speed_limit_mbps": {"type": "number", "description": "Download speed limit in MiB/s (0.5-1000); the server's default is 2.5."}}}},
    {"name": "server_cache",
     "description": "What Stremio's server has cached: folder, size on disk, free space, and the torrents it knows (marking the ones Jarvis downloads).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "server_cache_clear",
     "description": ("Empty Stremio's cache (asks the person first): stops the server Jarvis started, deletes what is inside its "
                     "stremio-cache folder, starts it again. Finished hub downloads are separate files and are not touched. "
                     "Refuses while a Stremio download is running."),
     "parameters": {"type": "object", "properties": {}}},
]

TOOLS = {
    "stack_status": tool_stack_status,
    "stack_start": tool_stack_start,
    "stack_stop": tool_stack_stop,
    "stack_doctor": tool_stack_doctor,
    "stremio_prepare": tool_stremio_prepare,
    "server_cache": tool_server_cache,
    "server_cache_clear": tool_server_cache_clear,
}

TOOL_GROUP = "stremio"

TOOL_KEYWORDS = {
    "stack_status": {"stremio status": 10, "is stremio running": 10, "is stremio up": 10, "stremio server status": 10,
                     "stremio stack": 9, "stremio server running": 9},
    "stack_start": {"start stremio": 10, "start the stremio server": 10, "start the stremio": 9, "launch stremio": 9,
                    "open the stremio server": 8, "bring stremio up": 9},
    "stack_stop": {"stop stremio": 10, "stop the stremio server": 10, "shut down stremio": 9, "close the stremio server": 9},
    "stack_doctor": {"stremio doctor": 10, "stremio problems": 9, "stremio isn't working": 10, "stremio is not working": 10,
                     "stremio not working": 10, "stremio server problems": 9},
    "stremio_prepare": {"prepare stremio": 10, "stremio cache unlimited": 10, "stremio cache size": 9, "stremio cache folder": 9,
                        "set up stremio for downloads": 10, "stremio speed limit": 8},
    "server_cache": {"stremio cache": 8, "what's in the stremio cache": 10, "stremio cache usage": 10, "how big is the stremio cache": 10},
    "server_cache_clear": {"clear the stremio cache": 10, "clear stremio cache": 10, "empty the stremio cache": 10,
                           "delete the stremio cache": 10},
}

TOOL_PACK_INSTRUCTION = (
    "Stremio's streaming server does the torrent work for the Download Hub: dl_add takes a magnet link, a 40-character "
    "infohash or a Stremio stream and the hub daemon downloads it through that server. stack_status says whether the "
    "server is up (it may already be open in the Stremio app: then nothing needs starting); stack_start brings it up; "
    "stremio_prepare (asks first) sets the cache to unlimited before the first download. For 'Stremio isn't working', use stack_doctor. "
    "Finding a show: media_find turns a name into an id and type (several titles match: ask the owner), media_episodes lists "
    "seasons and episodes, stream_search ranks one episode's releases. Ids may be IMDb (tt...) or Kitsu (kitsu:N): pass id and "
    "type exactly as given, never invent one. A release that doesn't state its language is not a dub. "
    "A whole season: bulk_plan shows a plan and queues nothing; read its message to the owner, ask about any episode with no "
    "match (the plan lists what exists), and only after they agree call bulk_run with the plan_id (it asks for confirmation "
    "itself). With no season named the plan takes the latest and says so: pass that on. "
    "To play: ONE play_episode call: play_episode takes a title or an IMDb id, season and episode, quality, audio and language; "
    "it finds, ranks and tries releases itself, plays in mpv/VLC, else opens stremio-web. After stream_search pass pick = the "
    "row's rank. \"Stream\"/\"watch\" = play now; \"download\" = dl_add. Ask the owner only when a tool says so. play_local plays a "
    "finished download; stremio_open opens stremio-web; play_control controls the mpv Jarvis opened. dl_add also takes the full "
    "path of a .torrent file."
)

# stremio_prepare changes the server's own settings; server_cache_clear stops it and deletes files.
TOOL_CONFIRM_REQUIRED = {"stremio_prepare", "server_cache_clear"}
TOOL_AI_REVIEW = set()

TOOL_COMMANDS = [
    {"name": "stack", "tool": "stack_status", "riskTier": "safe",
     "summary": "Is Stremio's server (and stremio-web) up?",
     "description": "Shows whether the Stremio streaming server, stremio-web and the download hub's daemon are running.",
     "example": "/stack"},
]

# ---------------------------------------------------------------------------
# the daemons (Menu > Daemons)
# ---------------------------------------------------------------------------

DAEMONS = [
    {"id": "server", "name": "Stremio streaming server",
     "description": "The torrent server the Download Hub's Stremio engine and playback use (127.0.0.1:11470). "
                    "Does nothing if the Stremio app already runs one.",
     "command": ["{python}", "{tool_dir}/_stremio_server.py"],
     "restart": "on-failure", "autostart": False, "categories": ["Stremio"]},
    {"id": "web", "name": "stremio-web",
     "description": "Stremio's web player (the screen playback links open). Set web.dir in ~/.jarvis/stremio.json.",
     "command": ["{python}", "{tool_dir}/_stremio_web.py"],
     "restart": "on-failure", "autostart": False, "categories": ["Stremio"]},
]

# ---------------------------------------------------------------------------
# Test Checklist
# ---------------------------------------------------------------------------

TEST_CHECKLIST_GROUP = {
    "label": "Stremio",
    "blurb": "The Stremio stack (server, stremio-web) and cache, finding shows and streams, season plans, playing: stack_*, stremio_prepare, server_cache*, stremio_open, stremio_addons, stremio_library, stremio_mark_watched, media_find, media_episodes, stream_search, bulk_plan, bulk_run, play_episode, play_local.",
}

TEST_CHECKLIST = {
    "stack_status": {
        "does": "Says whether Stremio's server, stremio-web and the hub daemon are up.",
        "steps": [
            {"run": {}, "expect": "ok: true with server / web / hub blocks and a one-line summary. With the Stremio app open, the server reads 'already up (started outside Jarvis)'. With it down, server.state / title / sentence say why in plain words (Installed, not running / Not found / App open, server silent / Running on another port / Port in use)."},
            {"ask": "Is Stremio running?", "expect": "Calls stack_status and answers from it."},
        ],
        "needs": ["Nothing; it works with the server down too (it says so)."],
        "watch": ["It never starts anything and never shows an account key."],
    },
    "stack_start": {
        "does": "Starts Stremio's streaming server (and stremio-web) as Jarvis daemons, unless one already answers.",
        "steps": [
            {"run": {"what": "server"}, "expect": "With the Stremio app open: server state 'already_up', nothing started (Menu > Daemons shows no new daemon running). With it closed: state 'started' with a version within the wait (hub setting stremio.start_wait_seconds, default 60 s). Stremio in a non-default folder: it is found without editing any file. With Stremio not installed: ok false and one sentence saying it wasn't found and what was looked at. With the app open but its server silent: ok false, 'the Stremio app is open, but its server isn't answering', and nothing is started."},
            {"run": {"what": "web"}, "expect": "'not_configured' with a note when web.dir isn't set; otherwise 'started' (first start can take a while to build)."},
            {"run": {"what": "bogus"}, "expect": "ok: false, 'what must be server, web or both'; nothing is started."},
            {"ask": "Start Stremio", "expect": "Calls stack_start and reports the state."},
        ],
        "needs": ["The Stremio desktop app installed (found automatically; or the paths saved with Locate...)"],
        "care": "Starting a server twice must never happen: check the Stremio app is closed OR that the answer says already_up.",
        "watch": ["Menu > Daemons shows 'Stremio streaming server' under the Stremio category.",
                  "Owner only: a chat guest is not offered stack_start (tools.OWNER_ONLY_TOOLS)."],
    },
    "stack_stop": {
        "does": "Stops the server / stremio-web that Jarvis started.",
        "steps": [
            {"run": {"what": "server"}, "expect": "A server Jarvis started stops (state 'stopped'). One the Stremio app started: state 'not_ours' and nothing is killed. While a download or stream is running it asks first. With no server running at all: ok: true, state 'not_running_by_jarvis' and nothing is killed."},
            {"run": {"what": "web"}, "expect": "ok: true; stremio-web that Jarvis started stops (state 'stopped'); with none started by Jarvis, state 'not_running_by_jarvis'."},
            {"run": {"what": "bogus"}, "expect": "ok: false, 'what must be server, web or both'; nothing is stopped."},
        ],
        "care": "Stopping the server interrupts running Stremio downloads; they retry once it is back.",
        "watch": ["Owner only: a chat guest is not offered stack_stop (tools.OWNER_ONLY_TOOLS)."],
    },
    "stack_doctor": {
        "does": "Checks the Stremio setup and says what is wrong.",
        "steps": [
            {"run": {}, "expect": "With Stremio not installed (the failure case): ok: false, each 'looked: ...' row is a warning saying what it did not find, and the 'state' row reads 'Not found: I couldn't find Stremio on this PC ...'. With a working setup: ok with a checks list: 'looked: port / process / registry / shortcut / folder' rows saying what each probe found, a 'state' row in plain words, server files, server version, cache size and folder, free space, stremio-web, player, daemons, the stremio engine; a copyable `diagnostic` text. The config block shows '(set)' for any account key, never its value."},
        ],
    },
    "stremio_prepare": {
        "does": "Sets the Stremio server's cache to unlimited (and optionally its folder, connections and speed limit).",
        "steps": [
            {"run": {"cache": "unlimited"}, "expect": "Asks first. Then ok: true with 'changed' showing cacheSize going to null, and stack_doctor afterwards shows cache unlimited. With the server not running (the failure case): ok: false, 'the Stremio server isn't answering at <url> (is it running? try stack_start). stremio_prepare changes the running server's settings; start it first (stack_start)'; nothing changes."},
            {"run": {"cache": "9999", "cache_root": "D:/<a drive with little space>"}, "expect": "ok: false, says the drive has too little free space (nothing changed)."},
            {"run": {"cache_root": "..\\x"}, "expect": "ok: false, cache_root must be a full path."},
            {"run": {"max_connections": 5}, "expect": "ok: false: 'max_connections must be between 10 and 500'; nothing changes."},
            {"run": {"speed_limit_mbps": 0}, "expect": "ok: false: 'speed_limit_mbps must be between 0.5 and 1000'; nothing changes."},
            {"run": {"cache": "banana"}, "expect": "ok: false: 'cache must be a number'; nothing changes."},
        ],
        "needs": ["The Stremio server running (stack_start)"],
        "care": "Changes the running server's own settings; the old cache folder is left in place.",
    },
    "server_cache": {
        "does": "Shows what the Stremio server has cached and which torrents it knows.",
        "steps": [
            {"run": {}, "expect": "ok: true with cache_root, size on disk, drive_free and a torrents list; rows Jarvis downloads say known_to_jarvis. With the server not running (the failure case): ok: false, 'the Stremio server isn't answering at <url> (is it running? try stack_start)' and a hint to start it."},
        ],
        "needs": ["The Stremio server running"],
    },
    "server_cache_clear": {
        "does": "Stops the Jarvis-started server, empties <cacheRoot>/stremio-cache and starts it again.",
        "steps": [
            {"run": {}, "expect": "Asks first. With a Stremio download running: ok false, says to pause it. With the server started by the Stremio app: ok false, asks you to close it. Otherwise ok: true with cleared_files and freed, and the server comes back. With no server ever started by Jarvis (cache folder unknown): ok: false, 'I need the server running once to read its cache folder (stack_start); I won't guess a folder to delete'; nothing is deleted."},
        ],
        "care": "Deletes files: only inside <cacheRoot>/stremio-cache, never through a link, never while the server runs. Finished hub downloads are separate files.",
        "watch": ["Owner only: a chat guest is not offered server_cache_clear (tools.OWNER_ONLY_TOOLS)."],
    },
}
