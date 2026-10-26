"""Update findings (N23): "a newer version of X exists", kept apart from download rows.

An UPDATE AVAILABLE is not a download: nothing is being fetched, and a pip or npm list can hold hundreds
of them.  So they are not rows.  The hub keeps a separate current list (table `updates`, hub/store.py) and
this module holds the rules around it and the runner that fills it.

  clean_finding()   one finding from a source -> capped, printable text (provider text is UNTRUSTED)
  parse_answer()    the answer of an update tool: {"updates": [...], "complete": bool, "problems": [...]}
  apply_answer()    apply one answer to the store and record how the check went
  present()         what dl_updates, the admin op and (later) the Updates tab show: findings + health in words
  request_check()   "check now": sets a flag the daemon's runner picks up (a tool call is another process)
  dismiss_finding() "Ignore this version"
  UpdateRunner      the checks themselves, on its OWN worker thread, started by the hub daemon

The rules the store keeps (hub/store.py `update_apply`, `_update_put`):

* The hub NEVER compares versions.  `installed` and `available` are the source's own words; "1.10" is not
  worked out to be newer than "1.9".  A source says "an update is available" and that is all the hub knows.
* The same id again REPLACES the finding.  `complete: true` means "this is every update I know of", so a
  finding the answer does not list is CLEARED (except one whose `source` is named in the answer's own
  `problems`, and nothing at all when part of the answer was unusable).  Without `complete`, only what
  arrives is updated.
* "Ignore this version" hides a finding until a DIFFERENT `available` text arrives.
* A provider switched off in the Providers tab (or with `update_checks` off) is not checked and shows nothing.
* A failed, timed-out or offline check keeps the old findings (they show their age) and says what failed.

The runner (what the owner asked for, N23e):

* its own thread: a one-minute check can never delay the poll of Steam rows (the poll runner is another
  object on the daemon's tick; the check runs in a thread of its own and the runner only schedules);
* the first check comes a few minutes after the daemon starts (never during start-up), then every
  `update_interval_h` per provider (24 by default, the owner's `update_interval_h` setting);
* one check at a time per provider; a second request while one runs is "already checking";
* a manual check is limited to one per provider every MANUAL_LIMIT_S;
* a check that takes longer than CHECK_TIMEOUT_S is given up on (its thread cannot be killed, so that
  provider is not checked again until the thread ends).

Nothing here changes anything on the machine: an update tool only reads, the hub never installs, upgrades or
removes anything, and there is no model-facing way to give a provider an update check.
"""

import json
import logging
import threading
import time

from . import settings as settings_mod, states

log = logging.getLogger("jarvis.hub.updates")

FIRST_CHECK_DELAY_S = 180.0          # "a few minutes after the daemon starts"
MANUAL_LIMIT_S = 300.0               # one manual check per provider every 5 minutes
CHECK_TIMEOUT_S = 60.0               # the longest list timeout pkg_tools uses today
LOOP_S = 2.0                         # how often the runner's own thread looks at the clock
SYNC_EVERY_S = 60.0                  # how often it re-reads which providers declare an update tool
RUNNING_STALE_S = CHECK_TIMEOUT_S + 30.0     # a "running" mark older than this belongs to a dead daemon
RETRY_BASE_S = 1800.0                # after a failed check: 30 min, doubling, never later than the interval
MAX_PROBLEMS = 10

MAX_TITLE = 200
MAX_VERSION = 80
MAX_SOURCE = 40
MAX_DETAIL_ROWS = 10
MAX_PROBLEM_TEXT = 160
MAX_SIZE = 2 ** 50


# --------------------------------------------------------------------------- cleaning

def _text(value):
    """A version as text: a string, or a number somebody wrote unquoted; anything else is nothing."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return str(value)
    return ""


def clean_finding(raw):
    """One finding from a source -> (local id, fields) or (None, why).

    Everything is provider text and untrusted: control characters out, one line, capped.  `size_bytes`
    is a whole number or nothing.  Nothing is parsed or compared."""
    if not isinstance(raw, dict):
        return None, "a finding must be an object"
    local = states.clean_text(raw.get("id"), 120)
    if not local:
        return None, "a finding needs an id"
    title = states.clean_text(raw.get("title"), MAX_TITLE) or local
    return local, {
        "title": title,
        "installed": states.clean_text(_text(raw.get("installed")), MAX_VERSION) or "",
        "available": states.clean_text(_text(raw.get("available")), MAX_VERSION) or "",
        "size_bytes": states.clean_int(raw.get("size_bytes"), 0, MAX_SIZE),
        "source": states.clean_text(_text(raw.get("source")), MAX_SOURCE) or "",
        "detail": states.clean_detail(raw.get("detail"))[:MAX_DETAIL_ROWS],
    }


def _clean_problems(value):
    """[{"source": "winget", "error": "timed out after 60 s"}] (or plain strings) -> a capped list of pairs."""
    out = []
    for entry in value if isinstance(value, (list, tuple)) else []:
        if len(out) >= MAX_PROBLEMS:
            break
        if isinstance(entry, dict):
            source = states.clean_text(entry.get("source"), MAX_SOURCE) or ""
            error = states.clean_text(entry.get("error") or entry.get("message"), MAX_PROBLEM_TEXT) or ""
        elif isinstance(entry, str):
            source, error = "", states.clean_text(entry, MAX_PROBLEM_TEXT) or ""
        else:
            continue
        if source or error:
            out.append({"source": source, "error": error or "it failed"})
    return out


def parse_answer(result):
    """An update tool's answer -> {"items", "complete", "problems", "error"}.  `error` is set when the check
    FAILED as a whole (nothing may be applied or cleared); `problems` are parts that failed inside an
    answer that otherwise arrived (one manager timed out, say)."""
    out = {"items": [], "complete": False, "problems": [], "error": None}
    if isinstance(result, (str, bytes)):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            out["error"] = "the check answered something that is not JSON"
            return out
    if not isinstance(result, dict):
        out["error"] = "the check answered something other than an object"
        return out
    if result.get("error"):
        out["error"] = states.clean_text(result["error"], 200)
        return out
    if result.get("ok") is False:
        out["error"] = "the check reported that it failed"
        return out
    items = result.get("updates")
    if not isinstance(items, list):
        out["error"] = "the check answer has no 'updates' list"
        return out
    out["items"] = items
    out["complete"] = result.get("complete") is True
    out["problems"] = _clean_problems(result.get("problems"))
    return out


# --------------------------------------------------------------------------- applying an answer

def _limit_kb(store):
    try:
        return int(store.get_settings()["provider_limits"]["max_report_kb"])
    except Exception:  # noqa: BLE001 - a limit that can't be read is the default, never a crash
        return 64


def apply_answer(store, provider_id, result, now=None):
    """Apply one answer and record how the check went.  Returns
    {"ok", "applied", "dropped", "cleared", "problems", "error"}; never raises.

    A failed answer (error, not JSON, no list, over the size limit) applies nothing and clears nothing: the
    old findings stay, and the state says what failed."""
    now = store.now() if now is None else now
    out = {"ok": False, "applied": 0, "dropped": 0, "cleared": 0, "problems": [], "error": None}
    try:
        parsed = parse_answer(result)
        if not parsed["error"]:
            size_kb = _limit_kb(store)
            if len(json.dumps(result, default=str)) > size_kb * 1024:
                parsed["error"] = "the check answer is over %d KB and was dropped" % size_kb
        if parsed["error"]:
            out["error"] = parsed["error"]
        else:
            problems = parsed["problems"]
            failed = [p["source"] for p in problems if p["source"]]
            # a problem that names no source gives no way to tell which findings it covers: clear nothing
            complete = parsed["complete"] and all(p["source"] for p in problems)
            applied = store.update_apply(provider_id, parsed["items"], complete=complete,
                                         failed_sources=failed, now=now)
            out.update({"applied": applied["applied"], "dropped": applied["dropped"], "cleared": applied["cleared"]})
            notes = list(applied["problems"])
            if applied["dropped"] and not notes:
                notes.append("%d item(s) of the answer were unusable and were skipped" % applied["dropped"])
            out["problems"] = problems + [{"source": "", "error": t} for t in notes]
            out["ok"] = True
        record_check(store, provider_id, now, out)
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "error": "applying the check failed: %s" % states.clean_text(exc, 150)})
        log.exception("applying an update check for %s failed", provider_id)
    return out


def record_check(store, provider_id, now, summary, keep_running=False):
    """Write how a check ended.  An answer (even one with problems) sets `checked_at`; a failure leaves it
    alone, so the age of the findings stays honest.  `ok_at` moves only when nothing at all went wrong."""
    fields = {}
    if not keep_running:
        fields["running_since"] = None
    if summary.get("error"):
        fields["error"] = summary["error"]
    else:
        problems = summary.get("problems") or []
        fields.update(error=None, checked_at=now, problems_json=json.dumps(problems))
        if not problems:
            fields["ok_at"] = now
    store.update_check_put(provider_id, **fields)


# --------------------------------------------------------------------------- reading it back

def age_words(seconds):
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 60:
        return "a moment ago"
    if s < 3600:
        return "%d min ago" % (s // 60)
    if s < 86400:
        return "%d h ago" % (s // 3600)
    days = s // 86400
    return "%d day%s ago" % (days, "" if days == 1 else "s")


def _is_running(row, now):
    running = (row or {}).get("running_since")
    return running is not None and now - float(running) < RUNNING_STALE_S


def _finding_out(f, now, with_hidden=False):
    out = {
        "provider": f["provider"], "provider_label": f["provider_label"], "id": f["local_id"],
        "title": f["title"], "installed": f["installed"], "available": f["available"],
        "size_bytes": f["size_bytes"], "source": f["source"], "detail": f["detail"],
        "checked_at": f["checked_at"], "age_s": max(0.0, now - f["checked_at"]), "first_seen_at": f["first_seen_at"],
    }
    if with_hidden:
        out["hidden"] = bool(f["hidden"])
    return out


def present(store, provider=None, now=None, include_hidden=False):
    """The stored findings and how each provider's checks are going, ready to show.

    Providers switched off, and providers with `update_checks` off, are left out completely (findings and
    health).  `problems` is a list of plain sentences; the text inside them is the provider's own and is
    capped, so a caller that puts it in a prompt must treat it as data."""
    now = store.now() if now is None else now
    wanted = str(provider or "").strip() or None
    checks = store.update_check_state()
    providers = {p["id"]: p for p in store.list_providers()}
    cfg = {pid: settings_mod.update_settings(p.get("settings"), (checks.get(pid) or {}).get("start_interval_h"))
           for pid, p in providers.items()}

    def shown(pid):
        p = providers.get(pid)
        if p is not None and not p["enabled"]:
            return False
        return bool(cfg[pid]["update_checks"]) if pid in cfg else True

    findings = [f for f in store.list_updates(provider=wanted, include_hidden=include_hidden) if shown(f["provider"])]
    rows = [_finding_out(f, now, with_hidden=include_hidden) for f in findings]
    per_provider = {}
    for f in findings:
        per_provider[f["provider"]] = per_provider.get(f["provider"], 0) + 1

    out_providers, problems = [], []
    for pid in sorted(set(checks) | set(per_provider)):
        if wanted and pid != wanted:
            continue
        if not shown(pid):
            continue
        row = checks.get(pid) or {}
        p = providers.get(pid) or {}
        label = states.clean_text(p.get("label") or pid, 60)
        has_source = bool(row.get("update_tool"))
        if not has_source and not per_provider.get(pid):
            continue
        checked_at = row.get("checked_at")
        if _is_running(row, now):
            status = "checking"
        elif row.get("error"):
            status = "failed"
        elif row.get("problems"):
            status = "partial"
        elif checked_at is None:
            status = "never" if has_source else "pushed"
        else:
            status = "ok"
        age = None if checked_at is None else max(0.0, now - checked_at)
        out_providers.append({
            "provider": pid, "label": label, "status": status, "can_check": has_source,
            "checked_at": checked_at, "age_s": age, "ok_at": row.get("ok_at"),
            "last_try_at": row.get("started_at"), "error": row.get("error"),
            "problems": list(row.get("problems") or []), "count": per_provider.get(pid, 0),
            "update_checks": cfg.get(pid, {}).get("update_checks", True),
            "update_interval_h": cfg.get(pid, {}).get("update_interval_h", settings_mod.UPDATE_INTERVAL_H_DEFAULT),
        })
        if status == "failed":
            tail = "; the findings shown are from %s" % age_words(age) if per_provider.get(pid) and age is not None else ""
            problems.append("%s: the last check failed (%s)%s" % (label, row["error"], tail))
        for item in row.get("problems") or []:
            where = "%s: " % item["source"] if item.get("source") else ""
            problems.append("%s: %s%s" % (label, where, item.get("error") or "it failed"))

    latest = max([p["checked_at"] for p in out_providers if p["checked_at"] is not None] or [None],
                 key=lambda v: -1 if v is None else v)
    try:
        alive = bool(store.daemon_info().get("alive"))
    except Exception:  # noqa: BLE001
        alive = False
    return {"ok": True, "count": len(rows), "checked_at": latest, "updates": rows,
            "providers": out_providers, "problems": problems, "daemon_alive": alive}


def count(store, now=None):
    """The badge: how many findings are showing."""
    try:
        return present(store, now=now)["count"]
    except Exception:  # noqa: BLE001
        return 0


def checkable(store):
    """{provider id: label} of the providers that have an update tool, are switched on and have update
    checks on (what 'Check now' can reach)."""
    checks = store.update_check_state()
    out = {}
    for p in store.list_providers():
        row = checks.get(p["id"]) or {}
        if row.get("update_tool") and p["enabled"] and \
                settings_mod.update_settings(p.get("settings"), row.get("start_interval_h"))["update_checks"]:
            out[p["id"]] = states.clean_text(p.get("label") or p["id"], 60)
    return out


# --------------------------------------------------------------------------- asking for a check / ignoring one

def request_check(store, provider=None, now=None):
    """Ask the daemon's runner to check now.  A tool call or the panel is not the daemon's process, so this
    only sets a flag (`update_checks.requested_at`) that the runner picks up within a couple of seconds.

    Refused when the daemon isn't running, when a check is already running ("already checking"), and when
    the provider was asked less than MANUAL_LIMIT_S ago.  Returns {"ok", "results": [{provider, label,
    status, message}]}; status is requested, already_checking, too_soon or not_checkable."""
    now = store.now() if now is None else now
    name = str(provider or "").strip() or None
    try:
        alive = bool(store.daemon_info().get("alive"))
    except Exception:  # noqa: BLE001
        alive = False
    if not alive:
        return {"ok": False, "code": "daemon_down",
                "error": "The Download Hub daemon isn't running, so nothing can check for updates right now. "
                         "Start it from Menu > Daemons."}
    ready = checkable(store)
    results = []
    if name and name not in ready:
        known = {p["id"]: p for p in store.list_providers()}
        if name not in known:
            why = "there is no provider called %s" % states.clean_text(name, 40)
        elif not known[name]["enabled"]:
            why = "the provider is switched off"
        elif not (store.update_check_state().get(name) or {}).get("update_tool"):
            why = "this provider has no update check"
        else:
            why = "update checks are switched off for this provider"
        return {"ok": False, "error": "Can't check %s: %s." % (states.clean_text(name, 40), why),
                "results": [{"provider": name, "label": name, "status": "not_checkable", "message": why}]}
    for pid, label in sorted(ready.items()):
        if name and pid != name:
            continue
        status, seconds = store.update_check_claim(pid, now=now, limit_s=MANUAL_LIMIT_S, stale_s=RUNNING_STALE_S)
        if status == "requested":
            results.append({"provider": pid, "label": label, "status": "requested",
                            "message": "the check starts within a few seconds"})
        elif status == "checking":
            results.append({"provider": pid, "label": label, "status": "already_checking",
                            "message": "already checking"})
        else:
            results.append({"provider": pid, "label": label, "status": "too_soon",
                            "message": "checked on request a moment ago; try again in %d min" % max(1, int(seconds // 60) + 1)})
    if not results:
        return {"ok": True, "results": [], "message": "No provider has an update check that is switched on."}
    return {"ok": True, "results": results}


def dismiss_finding(store, provider, local_id, now=None):
    """\"Ignore this version\".  Returns {"ok": True, "hidden": {...}} or {"ok": False, "error": ...}."""
    found = store.update_dismiss(provider, local_id, now=now)
    if found is None:
        return {"ok": False, "error": "there is no such update"}
    return {"ok": True, "hidden": {"provider": found["provider"], "id": found["local_id"],
                                   "title": found["title"], "available": found["available"]},
            "note": "It stays hidden until the source reports a different version."}


# --------------------------------------------------------------------------- the runner

class _Check:
    """One check in flight, in a thread of its own."""

    def __init__(self, provider_id, started):
        self.provider = provider_id
        self.started = started
        self.result = None
        self.error = None
        self.done = threading.Event()
        self.thread = None


def _declared_default():
    from .. import tools
    return list(getattr(tools, "AUTO_DOWNLOADS", []) or [])


class UpdateRunner:
    """Schedules the update checks.  Own thread (`start`), or driven by hand with `tick(now)` in tests.

    `executor(tool name, args)` runs an update tool (None = tools.execute_tool); `declared()` lists the
    TOOL_DOWNLOADS entries (None = tools.AUTO_DOWNLOADS, read on the runner's own thread: reading the tool
    registry takes seconds and must never hold up the daemon's tick)."""

    def __init__(self, store, clock=time.time, executor=None, declared=None, first_delay_s=FIRST_CHECK_DELAY_S,
                 timeout_s=CHECK_TIMEOUT_S, loop_s=LOOP_S):
        self.store = store
        self._clock = clock
        self._execute = executor
        self._declared = declared
        self._first_delay = float(first_delay_s)
        self._timeout = float(timeout_s)
        self._loop = float(loop_s)
        self._started = clock()
        self._synced = None
        self._state = {}                 # provider id -> {"next", "fails", "check", "stuck"}
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ the thread

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._started = self._clock()
        self._thread = threading.Thread(target=self._run, name="hub-updates", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout=2.0):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self):
        try:
            self.store.update_check_reset_running()
        except Exception:  # noqa: BLE001
            log.exception("resetting the update checks failed")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("the update runner's pass failed")
            self._stop.wait(self._loop)

    # ------------------------------------------------------------------ which providers can be checked

    def _declared_entries(self):
        try:
            return list((self._declared or _declared_default)())
        except Exception:  # noqa: BLE001 - unreadable: keep what is stored, never wipe it
            log.exception("couldn't read the declared providers for the update checks")
            return None

    def sync_sources(self):
        """Record which providers declare an `update_tool` (and their starting interval) in the store, so the
        tool, the panel and the admin op can answer without loading the tool registry.  A provider that no
        longer declares one loses it."""
        entries = self._declared_entries()
        if entries is None:
            return []
        have = {}
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") and entry.get("update_tool"):
                have[entry["id"]] = entry
        for pid, entry in have.items():
            self.store.update_check_put(pid, update_tool=entry["update_tool"],
                                        start_interval_h=entry.get("update_interval_h"))
        for pid, row in self.store.update_check_state().items():
            if row.get("update_tool") and pid not in have:
                self.store.update_check_put(pid, update_tool=None, start_interval_h=None)
        return sorted(have)

    def _tool_executor(self):
        if self._execute is not None:
            return self._execute
        from .. import tools
        return lambda name, args: tools.execute_tool(name, args)

    # ------------------------------------------------------------------ one pass

    def tick(self, now=None):
        """One pass.  Starts the checks that are due, collects the ones that finished, gives up on the ones
        that took too long.  Returns the provider ids it did something for."""
        now = self._clock() if now is None else now
        acted = []
        if self._synced is None or now - self._synced >= SYNC_EVERY_S:
            self._synced = now
            self.sync_sources()
        providers = {p["id"]: p for p in self.store.list_providers()}
        for pid, row in self.store.update_check_state().items():
            st = self._state.get(pid)
            if not row.get("update_tool") and (st is None or (st["check"] is None and st["stuck"] is None)):
                continue
            if st is None:
                st = self._state[pid] = {"next": self._started + self._first_delay, "fails": 0,
                                         "check": None, "stuck": None}
            if st["check"] is not None:
                check = st["check"]
                if check.done.is_set():
                    self._collect(pid, st, now)
                    acted.append(pid)
                elif now - check.started > self._timeout:
                    self._time_out(pid, st, now)
                    acted.append(pid)
                continue
            if st["stuck"] is not None:
                if not st["stuck"].done.is_set():
                    continue                    # the old check is still hanging: never stack a second one
                st["stuck"] = None
                self.store.update_check_put(pid, running_since=None)
            if not row.get("update_tool"):
                continue
            provider = providers.get(pid)
            if provider is None or not provider["enabled"]:
                continue                        # switched off: not checked, shows nothing
            cfg = settings_mod.update_settings(provider.get("settings"), row.get("start_interval_h"))
            if not cfg["update_checks"]:
                continue
            asked = row.get("requested_at") is not None      # a manual check was asked for (cleared in _begin)
            if not asked and now < st["next"]:
                continue
            self._begin(pid, row, st, now)
            acted.append(pid)
        return acted

    def _begin(self, pid, row, st, now):
        tool = row["update_tool"]
        check = _Check(pid, now)
        self.store.update_check_put(pid, running_since=now, started_at=now, requested_at=None)
        run = self._tool_executor()

        def work():
            try:
                check.result = run(tool, {})
            except BaseException as exc:  # noqa: BLE001 - whatever the tool does, it ends in a recorded failure
                check.error = states.clean_text(exc, 150) or "it raised an error"
            finally:
                check.done.set()

        check.thread = threading.Thread(target=work, name="hub-update-check-%s" % pid, daemon=True)
        st["check"] = check
        check.thread.start()

    def _collect(self, pid, st, now):
        check, st["check"] = st["check"], None
        if check.error:
            summary = {"ok": False, "error": "the check failed: %s" % check.error}
            record_check(self.store, pid, now, summary)
        else:
            summary = apply_answer(self.store, pid, check.result, now=now)
        self._schedule_next(pid, st, now, summary["ok"])

    def _time_out(self, pid, st, now):
        check, st["check"], st["stuck"] = st["check"], None, st["check"]
        summary = {"ok": False, "error": "the check took longer than %d s and was given up on" % int(self._timeout)}
        record_check(self.store, pid, now, summary, keep_running=True)
        self._schedule_next(pid, st, now, False)
        return check

    def _schedule_next(self, pid, st, now, ok):
        try:
            provider = self.store.get_provider(pid) or {}
            row = self.store.update_check_state().get(pid) or {}
            interval_s = settings_mod.update_settings(provider.get("settings"), row.get("start_interval_h"))[
                "update_interval_h"] * 3600.0
        except Exception:  # noqa: BLE001
            interval_s = settings_mod.UPDATE_INTERVAL_H_DEFAULT * 3600.0
        if ok:
            st["fails"] = 0
            st["next"] = now + interval_s
        else:
            st["fails"] += 1
            st["next"] = now + min(interval_s, RETRY_BASE_S * (2 ** min(st["fails"] - 1, 4)))
