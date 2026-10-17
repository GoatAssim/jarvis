"""hub_doctor (plan section 10.1): is the hub healthy, and if not, what is wrong?

Each check is {name, status: ok | warn | fail, detail}.  `ok` for the whole report means
"no fail".  A warn is something the owner should know (the daemon isn't running, the disk
is nearly full); a fail is something that stops downloads working.

Extension points for later phases, so nobody edits this file to add a prerequisite:
  * an engine reports its own prerequisites through `available()` (ffmpeg for yt-dlp, the
    Stremio server for the Stremio engine) -- they appear under "engine <name>";
  * an engine may also define `doctor() -> [ {name, status, detail}, ... ]` for more.
"""

import os
import shutil

from . import SCHEMA_VERSION, HUB_VERSION, engines, paths, service, settings as settings_mod
from .store import Store

LOW_DISK_BYTES = 5 * 1024 ** 3


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _gb(n):
    return "%.1f GB" % (n / 1024 ** 3)


def run(store=None):
    checks = []
    store = store or Store()

    # --- the store
    try:
        version = store.schema_version()
        journal = store.journal_mode()
        quick = store.quick_check()
        if version != SCHEMA_VERSION:
            checks.append(_check("store", "fail", "schema version %s, expected %s" % (version, SCHEMA_VERSION)))
        elif quick != "ok":
            checks.append(_check("store", "fail", "sqlite integrity check says: %s" % quick))
        elif str(journal).lower() != "wal":
            checks.append(_check("store", "warn", "opened, but not in WAL mode (%s); the panel may wait on the daemon" % journal))
        else:
            checks.append(_check("store", "ok", "%s (WAL, schema %d)" % (paths.db_path(), version)))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("store", "fail", "can't open %s: %s" % (paths.db_path(), exc)))
        return _report(checks)          # nothing else can be read without the store

    settings = store.get_settings()
    counts = store.counts()

    # --- the daemon
    daemon = service.daemon_view(store)
    # Hub-engine rows only: a provider's rows are somebody else's transfer and go stale instead.
    running_rows = sum(b["active"] for b in counts["by_engine"].values())
    if daemon["alive"]:
        checks.append(_check("daemon", "ok", "running (pid %s, last beat %s s ago)" % (daemon["pid"], daemon["age_s"])))
    elif running_rows > 0:
        checks.append(_check("daemon", "fail", "not running, but %d download(s) are marked as running; they will "
                             "restart when it does (Menu > Daemons > Download hub)" % running_rows))
    else:
        checks.append(_check("daemon", "warn", "not running: nothing will start until it is "
                             "(Menu > Daemons > Download hub)"))

    # --- where files go
    root = settings_mod.library_root(settings)
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not os.access(str(probe), os.W_OK):
        checks.append(_check("library root", "fail", "%s isn't writable" % probe))
    else:
        try:
            free = shutil.disk_usage(str(probe)).free
            status = "warn" if free < LOW_DISK_BYTES else "ok"
            note = "" if root.exists() else " (will be created on the first download)"
            checks.append(_check("library root", status, "%s%s, %s free" % (root, note, _gb(free))))
        except OSError as exc:
            checks.append(_check("library root", "warn", "%s: can't read free space (%s)" % (root, exc)))

    # --- engines
    for name, info in engines.availability().items():
        checks.append(_check("engine %s" % name, "ok" if info["available"] else "warn",
                             "ready" if info["available"] else info["reason"] or "not available"))
        engine = engines.get(name)
        extra = getattr(engine, "doctor", None)
        if callable(extra):
            try:
                for row in extra() or []:
                    checks.append(_check(str(row.get("name", name)), row.get("status", "warn"), str(row.get("detail", ""))))
            except Exception as exc:  # noqa: BLE001
                checks.append(_check("engine %s" % name, "warn", "its own checks failed: %s" % exc))
    for name, why in engines.LOAD_ERRORS.items():
        checks.append(_check("engine module %s" % name, "fail", "didn't load: %s" % why))

    # --- providers
    for provider in store.list_providers():
        label = "provider %s" % provider["id"]
        if not provider["enabled"]:
            checks.append(_check(label, "ok", "switched off by the owner"))
        elif provider.get("last_error"):
            checks.append(_check(label, "warn", provider["last_error"]))
        else:
            checks.append(_check(label, "ok", "%s mode, %s" % (
                provider["mode"], "no reports yet" if not provider.get("last_report_at") else "reporting")))

    # --- spooled reports nobody applied
    try:
        spooled = len(list(paths.inbox_dir().glob("*.json"))) if paths.inbox_dir().is_dir() else 0
    except OSError:
        spooled = 0
    if spooled:
        checks.append(_check("inbox", "warn" if daemon["alive"] else "fail",
                             "%d report(s) waiting to be applied%s" % (
                                 spooled, "" if daemon["alive"] else " (the daemon isn't running)")))
    checks.append(_check("hub version", "ok", HUB_VERSION))
    return _report(checks)


def _report(checks):
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        summary = "%d problem(s): %s" % (len(fails), "; ".join("%s: %s" % (c["name"], c["detail"]) for c in fails))
    elif warns:
        summary = "working, with %d warning(s): %s" % (len(warns), "; ".join(c["name"] for c in warns))
    else:
        summary = "all good"
    return {"ok": not fails, "summary": summary, "checks": checks}
