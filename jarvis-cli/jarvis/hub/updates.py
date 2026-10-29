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

An update tool only reads.  APPLYING an update (the Updates rework, contract v1) is a second, separate step:

  request_apply()   "apply this finding" / "apply all": marks the findings queued in the store (the daemon is another
                    process); refused when the daemon is down
  dry_run()         asks the provider's apply tool for a PLAN (`dry_run: true`: it changes nothing)
  ApplyRunner       the daemon's own thread: takes ONE queued apply at a time, calls the provider's own apply tool
                    (never a shell string, never a package-manager call built here), records done / started / failed,
                    leaves a row in the event trail, re-checks the provider, and ends in ONE notification per cycle
  auto_update_pass()  after a finished check, queues what the owner allowed to update by itself

Rules the apply side keeps:

* The hub NEVER builds an install command.  It calls the provider's declared `apply_tool` with `{"id": <the finding's
  id exactly as the source gave it>, "dry_run": false, **apply_args}` and believes what the tool answers about its own
  state; it never decides an update "worked" by comparing versions.
* Auto-update needs ALL of: the master switch (`updates.auto_update`), the provider's own `auto_update` tick, the
  provider's update checks on, the finding's group in `updates.include_groups`, a daemon that is alive, a usable apply
  tool; a hidden ("ignored") finding is never applied, nor is the same version that already failed once; at most
  `updates.auto_max_per_cycle` are queued per step.  A manual apply needs none of the switches: it is the owner's click.
* One apply at a time, over every provider.  A call that outlives its timeout is given up on and recorded as failed; its
  thread cannot be killed, so that provider is not applied to again until it ends (other providers go on, and checks are
  never held up by an apply).
* Nothing is applied for a guest: every way in (the admin op, the chat tool) refuses before it queues anything.
"""

import json
import logging
import threading
import time
import uuid

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
APPLY_LOOP_S = 1.0                   # how often the apply runner looks for a queued apply
DRY_RUN_TIMEOUT_S = 60.0             # a plan is a read: it gets as long as a check
MAX_MANUAL_BATCH = 200               # "apply all" queues at most this many findings in one request
MAX_APPLY_MESSAGE = 200
MAX_PLAN = 800
APPLY_STATES_OK = ("done", "started", "unchanged")
APPLY_TIMEOUT_DEFAULT_S = 600.0      # tool_downloads.APPLY_TIMEOUT_DEFAULT_S: the provider may declare 30 to 3600

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
    size = states.clean_int(raw.get("size_bytes"), 0, None)      # clean_int clamps to `hi`; an absurd size is no size
    if size is not None and size > MAX_SIZE:
        size = None
    return local, {
        "title": title,
        "installed": states.clean_text(_text(raw.get("installed")), MAX_VERSION) or "",
        "available": states.clean_text(_text(raw.get("available")), MAX_VERSION) or "",
        "size_bytes": size,
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
    """Shown as 'checking' only while the check is inside its timeout.  After that the runner has given up on
    it (the mark is kept so a manual request still says 'already checking' until the hung thread ends), and
    the health line must say what happened, not 'checking'."""
    running = (row or {}).get("running_since")
    return running is not None and now - float(running) < CHECK_TIMEOUT_S


def _group_of(row):
    group = (row or {}).get("update_group")
    return group if group in settings_mod.UPDATE_GROUPS else "other"


def _apply_view(info, finding):
    """What a finding shows about applying: {"state", "message", "at"} or None.  A row recorded for ANOTHER version than
    the one now listed is history, not the finding's state (unless it is still queued or running)."""
    if not info:
        return None
    if info["state"] not in ("queued", "running") and (info.get("available") or "") != (finding.get("available") or ""):
        return None
    return {"state": info["state"], "message": info.get("message") or "", "at": info.get("at")}


def _finding_out(f, now, with_hidden=False, group="other", can_apply=False, apply=None):
    out = {
        "provider": f["provider"], "provider_label": f["provider_label"], "id": f["local_id"],
        "title": f["title"], "installed": f["installed"], "available": f["available"],
        "size_bytes": f["size_bytes"], "source": f["source"], "detail": f["detail"],
        "checked_at": f["checked_at"], "age_s": max(0.0, now - f["checked_at"]), "first_seen_at": f["first_seen_at"],
        "group": group, "can_apply": bool(can_apply), "apply": apply,
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

    def usable(pid):
        """Can the hub apply this provider's findings: it declared an apply tool and that tool's schema is fine."""
        row = checks.get(pid) or {}
        return bool(row.get("apply_tool")) and not row.get("apply_problem")

    applying = store.applying_info()
    findings = [f for f in store.list_updates(provider=wanted, include_hidden=include_hidden) if shown(f["provider"])]
    rows = [_finding_out(f, now, with_hidden=include_hidden, group=_group_of(checks.get(f["provider"])),
                         can_apply=usable(f["provider"]),
                         apply=_apply_view(applying.get((f["provider"], f["local_id"])), f))
            for f in findings]
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
            "group": _group_of(row), "can_apply": usable(pid), "apply_problem": row.get("apply_problem"),
            "updates_only": bool(row.get("updates_only")),
            "auto_update": settings_mod.provider_auto_update(p.get("settings")),
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
    try:
        global_cfg = settings_mod.update_global_settings(store.get_settings())
        queued, running = store.applying_counts()
    except Exception:  # noqa: BLE001 - the panel still shows the findings
        global_cfg, queued, running = settings_mod.update_global_settings({}), 0, 0
    groups = []
    for gid in settings_mod.UPDATE_GROUPS:
        in_group = [r for r in rows if r["group"] == gid]
        groups.append({
            "id": gid, "label": settings_mod.UPDATE_GROUP_LABELS[gid],
            "included": gid in global_cfg["include_groups"], "count": len(in_group),
            "applicable": sum(1 for r in in_group if r["can_apply"] and not (
                r["apply"] and r["apply"]["state"] in ("queued", "running"))),
            "providers": sorted({p["provider"] for p in out_providers if p["group"] == gid}),
        })
    return {"ok": True, "count": len(rows), "checked_at": latest, "updates": rows,
            "providers": out_providers, "problems": problems, "daemon_alive": alive,
            "groups": groups, "settings": global_cfg, "applying": queued + running}


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


# --------------------------------------------------------------------------- applying (the Updates rework)

def _line(value, cap):
    return states.clean_text(value, cap) or ""


def parse_apply_result(result, dry_run=False):
    """An apply tool's answer -> {"ok", "state", "message", "plan", "needs_restart", "no_change"}.

    `state` is what the hub records for the finding: done, started or failed.  The tool's own `unchanged` ("nothing
    needed doing") is recorded as done with `no_change` True (the runner does not re-check for it).  Success needs
    `ok: true` AND a state the contract knows; an answer that is not JSON, has an `error`, says `ok: false`, says
    `state: "failed"` or names no state is a failure.  For `dry_run` only `ok` and a plan matter.  Provider text is
    untrusted: one line, capped."""
    out = {"ok": False, "state": "failed", "message": "", "plan": "", "needs_restart": False, "no_change": False}
    if isinstance(result, (str, bytes)):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            out["message"] = "the apply tool answered something that is not JSON"
            return out
    if not isinstance(result, dict):
        out["message"] = "the apply tool answered something other than an object"
        return out
    if result.get("error"):
        out["message"] = _line(result["error"], MAX_APPLY_MESSAGE)
        return out
    message = _line(result.get("message"), MAX_APPLY_MESSAGE)
    if result.get("ok") is not True:
        out["message"] = message or "the apply tool reported that it failed"
        return out
    if dry_run:
        plan = _line(result.get("plan"), MAX_PLAN) or message
        out.update(ok=True, state="plan", plan=plan or "the apply tool gave no plan", message=message)
        return out
    state = result.get("state")
    if state == "failed":
        out["message"] = message or "the apply tool reported that it failed"
        return out
    if state not in APPLY_STATES_OK:
        out["message"] = "the apply tool answered ok without a state (done, started or unchanged)"
        return out
    needs_restart = result.get("needs_restart") is True
    if not message:
        message = {"done": "applied", "started": "started: finish it in the program", "unchanged": "nothing needed doing"}[state]
    if needs_restart:
        message = _line("%s (a restart is needed)" % message, MAX_APPLY_MESSAGE)
    out.update(ok=True, state="started" if state == "started" else "done", message=message,
               needs_restart=needs_restart, no_change=(state == "unchanged"))
    return out


def _call_with_timeout(fn, timeout_s, name):
    """Run fn() in a thread; (finished, result, error).  A call that outlives the timeout is left running (a thread
    cannot be killed) and reported as not finished."""
    box = {"result": None, "error": None}
    done = threading.Event()

    def work():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - whatever the tool does, it ends in a recorded failure
            box["error"] = states.clean_text(exc, 150) or "it raised an error"
        finally:
            done.set()

    thread = threading.Thread(target=work, name=name, daemon=True)
    thread.start()
    finished = done.wait(timeout_s)
    return finished, box["result"], box["error"], done


def _apply_args(row, local_id, dry_run):
    """The arguments of one apply call: the declared `apply_args` first, then the hub's own two, which always win."""
    args = dict(row.get("apply_args") or {})
    args["id"] = local_id
    args["dry_run"] = bool(dry_run)
    return args


def _cannot_apply_reason(store, view_row, provider_id):
    """One sentence for why a finding can't be applied by the hub."""
    check = store.update_check_state().get(provider_id) or {}
    if check.get("apply_problem"):
        return "this provider's apply tool can't be used: %s" % _line(check["apply_problem"], 150)
    return "this provider can't apply updates by itself; update it the usual way"


def _lookup_finding(store, provider, local_id, now):
    """(view row, None) or (None, why) for ONE finding named by provider and id (hidden ones included)."""
    prov = store.get_provider(provider)
    if prov is None:
        return None, "there is no provider called %s" % _line(provider, 40)
    if not prov["enabled"]:
        return None, "the provider is switched off"
    check = store.update_check_state().get(provider) or {}
    cfg = settings_mod.update_settings(prov.get("settings"), check.get("start_interval_h"))
    if not cfg["update_checks"]:
        return None, "update checks are switched off for this provider"
    for row in present(store, provider=provider, now=now, include_hidden=True)["updates"]:
        if row["id"] == local_id:
            return row, None
    return None, "there is no such update"


def request_apply(store, provider=None, local_id=None, all_updates=False, group=None, origin="manual", now=None):
    """Queue findings to be applied.  A tool call or the panel is not the daemon's process, so this only marks them
    queued in the store (`update_applies`); the daemon's ApplyRunner picks them up.

    Refused when the daemon isn't running.  `local_id` (with `provider`) names ONE finding; `all_updates` queues every
    applicable one (optionally of one `provider` and/or one `group`), at most MAX_MANUAL_BATCH.  No setting is needed:
    this is the owner's click (or the owner's confirmed chat call).  Returns {"ok", "batch", "queued", "results": [{provider,
    id, title, status, message}]}; status is queued, already_running, not_applicable or refused."""
    now = store.now() if now is None else now
    name = str(provider or "").strip() or None
    lid = states.clean_text(local_id, 120) if local_id is not None else None
    try:
        alive = bool(store.daemon_info().get("alive"))
    except Exception:  # noqa: BLE001
        alive = False
    if not alive:
        return {"ok": False, "code": "daemon_down",
                "error": "The Download Hub daemon isn't running, so nothing can be applied right now. "
                         "Start it from Menu > Daemons."}
    if group is not None and group not in settings_mod.UPDATE_GROUPS:
        return {"ok": False, "error": "unknown group %r; use one of %s" % (group, ", ".join(settings_mod.UPDATE_GROUPS))}
    if lid is None and not all_updates:
        return {"ok": False, "error": "name one update (its provider and id) or ask for all of them"}
    if lid is not None and not name:
        return {"ok": False, "error": "an update is named by its provider and its id"}

    batch = "b-" + uuid.uuid4().hex[:10]
    if lid is not None:
        row, why = _lookup_finding(store, name, lid, now)
        if row is None:
            return {"ok": False, "error": "Can't apply %s: %s." % (_line(lid, 60), why),
                    "results": [{"provider": name, "id": lid, "title": lid, "status": "not_applicable", "message": why}]}
        targets = [row]
    else:
        view = present(store, provider=name, now=now)["updates"]
        targets = [r for r in view if (group is None or r["group"] == group) and r["can_apply"]]
        targets.sort(key=lambda r: (r["provider"], r["title"].lower(), r["id"]))
        targets = targets[:MAX_MANUAL_BATCH]

    results, queued = [], 0
    for row in targets:
        entry = {"provider": row["provider"], "id": row["id"], "title": row["title"]}
        if not row["can_apply"]:
            why = _cannot_apply_reason(store, row, row["provider"])
            entry.update(status="not_applicable", message=why)
        elif row.get("hidden"):
            entry.update(status="refused", message="this version is ignored; show it again first")
        else:
            status, _info = store.applying_queue(row["provider"], row["id"], batch=batch, origin=origin, now=now)
            if status == "queued":
                queued += 1
                entry.update(status="queued", message="queued: the hub applies it in a moment")
                store.add_event("update:%s" % row["provider"], "info", "apply queued%s: %s" % (
                    " by auto-update" if origin == "auto" else "", row["title"]))
            elif status == "already_running":
                entry.update(status="already_running", message="already queued or running")
            else:
                entry.update(status="refused", message="it is no longer listed" if status == "missing"
                             else "this version is ignored; show it again first")
        results.append(entry)
    if lid is not None and results and results[0]["status"] in ("not_applicable", "refused"):
        return {"ok": False, "error": "Can't apply %s: %s." % (_line(results[0]["title"], 60), results[0]["message"]),
                "results": results, "queued": 0, "batch": None}
    out = {"ok": True, "queued": queued, "batch": batch if queued else None, "results": results}
    if not results:
        out["message"] = "Nothing to apply."
    return out


def dry_run(store, provider, local_id, executor=None, timeout_s=DRY_RUN_TIMEOUT_S, now=None):
    """Ask the provider's apply tool for a PLAN of one finding (`dry_run: true`: the tool changes nothing).  Runs here,
    in the caller's process, and waits at most `timeout_s`.  Returns {"ok", "provider", "id", "title", "plan",
    "message"} or {"ok": False, "error"}."""
    now = store.now() if now is None else now
    name = str(provider or "").strip()
    lid = states.clean_text(local_id, 120) or ""
    if not name or not lid:
        return {"ok": False, "error": "a plan is asked for by an update's provider and id"}
    row, why = _lookup_finding(store, name, lid, now)
    if row is None:
        return {"ok": False, "error": "Can't plan %s: %s." % (_line(lid, 60), why)}
    if not row["can_apply"]:
        return {"ok": False, "error": "Can't plan %s: %s." % (_line(row["title"], 60),
                                                              _cannot_apply_reason(store, row, name))}
    check = store.update_check_state().get(name) or {}
    tool = check["apply_tool"]
    run = executor
    if run is None:
        from .. import tools
        run = lambda t, a: tools.execute_tool(t, a)  # noqa: E731
    args = _apply_args(check, lid, True)
    finished, result, error, _done = _call_with_timeout(lambda: run(tool, args), timeout_s, "hub-apply-plan-%s" % name)
    if not finished:
        return {"ok": False, "error": "The plan took longer than %d s and was given up on." % int(timeout_s)}
    if error:
        return {"ok": False, "error": "The plan failed: %s" % error}
    parsed = parse_apply_result(result, dry_run=True)
    if not parsed["ok"]:
        return {"ok": False, "error": "No plan: %s" % (parsed["message"] or "the apply tool said no")}
    return {"ok": True, "provider": name, "id": lid, "title": row["title"], "plan": parsed["plan"],
            "message": parsed["message"]}


def auto_update_pass(store, provider_id, now=None):
    """After a finished check: queue the findings the owner allowed to update by themselves.  Returns how many were queued.

    ALL of these must hold, else nothing is queued: the master switch (`updates.auto_update`), the provider's own
    `auto_update` tick, the provider switched on with update checks on, a usable apply tool, the provider's group in
    `updates.include_groups`, and a daemon that is alive.  A hidden finding is never queued; neither is a version that
    was already tried (done, started, failed or still queued: a failed version is never retried by itself, a newer
    `available` text gets a fresh try).  At most `updates.auto_max_per_cycle` per pass."""
    now = store.now() if now is None else now
    pid = str(provider_id or "")
    try:
        cfg = settings_mod.update_global_settings(store.get_settings())
        if not cfg["auto_update"]:
            return 0
        provider = store.get_provider(pid)
        check = store.update_check_state().get(pid) or {}
        if provider is None or not provider["enabled"] or not settings_mod.provider_auto_update(provider.get("settings")):
            return 0
        if not settings_mod.update_settings(provider.get("settings"), check.get("start_interval_h"))["update_checks"]:
            return 0
        if not check.get("apply_tool") or check.get("apply_problem") or _group_of(check) not in cfg["include_groups"]:
            return 0
        if not store.daemon_info().get("alive"):
            return 0
        tried = store.applying_info(pid)
        batch = "a-" + uuid.uuid4().hex[:10]
        queued = 0
        for f in store.list_updates(provider=pid):
            if queued >= cfg["auto_max_per_cycle"]:
                break
            row = tried.get((pid, f["local_id"]))
            if row is not None and (row["state"] in ("queued", "running") or (row.get("available") or "") == (f["available"] or "")):
                continue
            status, _info = store.applying_queue(pid, f["local_id"], batch=batch, origin="auto", now=now)
            if status == "queued":
                queued += 1
                store.add_event("update:%s" % pid, "info", "apply queued by auto-update: %s" % f["title"])
        return queued
    except Exception:  # noqa: BLE001 - a failed automatic step never breaks the check runner
        log.exception("the auto-update step for %s failed", pid)
        return 0


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


def _schemas_default():
    from .. import tools
    return list(getattr(tools, "TOOL_SCHEMAS", []) or [])


class UpdateRunner:
    """Schedules the update checks.  Own thread (`start`), or driven by hand with `tick(now)` in tests.

    `executor(tool name, args)` runs an update tool (None = tools.execute_tool); `declared()` lists the
    TOOL_DOWNLOADS entries (None = tools.AUTO_DOWNLOADS, read on the runner's own thread: reading the tool
    registry takes seconds and must never hold up the daemon's tick)."""

    def __init__(self, store, clock=time.time, executor=None, declared=None, first_delay_s=FIRST_CHECK_DELAY_S,
                 timeout_s=CHECK_TIMEOUT_S, loop_s=LOOP_S, schemas=None):
        self.store = store
        self._clock = clock
        self._execute = executor
        self._declared = declared
        self._schemas = schemas          # () -> list of tool schemas; read only when a provider declares an apply_tool
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
            apply_tool, problem = self._apply_declaration(entry)
            group = entry.get("update_group")
            self.store.update_check_put(
                pid, update_tool=entry["update_tool"], start_interval_h=entry.get("update_interval_h"),
                update_group=group if group in settings_mod.UPDATE_GROUPS else "other",
                update_args_json=json.dumps(entry.get("update_args") or {}),
                apply_tool=apply_tool, apply_args_json=json.dumps(entry.get("apply_args") or {}),
                apply_timeout_s=entry.get("apply_timeout_s") if apply_tool else None,
                updates_only=1 if entry.get("updates_only") else 0, apply_problem=problem)
        for pid, row in self.store.update_check_state().items():
            if row.get("update_tool") and pid not in have:
                self.store.update_check_put(pid, update_tool=None, start_interval_h=None, update_group=None,
                                            update_args_json=None, apply_tool=None, apply_args_json=None,
                                            apply_timeout_s=None, updates_only=0, apply_problem=None)
        return sorted(have)

    def _apply_declaration(self, entry):
        """(apply tool or None, problem or None) for one declared entry.  The loader may already have checked the apply
        tool's schema (tool_downloads.apply_schema_problem); it is checked again here, so a provider whose apply tool
        can't take `id` and `dry_run` is never given the chance to apply."""
        tool = entry.get("apply_tool")
        if not tool:
            return None, None
        try:
            from .. import tool_downloads
            problem = tool_downloads.apply_schema_problem(tool, (self._schemas or _schemas_default)())
        except Exception:  # noqa: BLE001 - unreadable schemas: not usable, said plainly
            log.exception("couldn't read the tool schemas to check %s", tool)
            problem = "its schema could not be read"
        if problem:
            return None, "%s: %s" % (tool, problem)
        return tool, None

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
        args = dict(row.get("update_args") or {})

        def work():
            try:
                check.result = run(tool, dict(args))
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
            if summary["ok"]:
                auto_update_pass(self.store, pid, now=now)
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


class ApplyRunner:
    """Applies queued findings, ONE at a time, on a thread of its own.  Own thread (`start`), or driven by hand with
    `tick(now)` in tests.  The checks are another thread (UpdateRunner): an apply never holds one up.

    `executor(tool name, args)` runs an apply tool (None = tools.execute_tool, which still refuses a tool the owner
    switched off in the Tool Manager: that is recorded as a failed apply); `deliver` is the notifier's deliver
    function (None = notify.default_deliver).  Every apply ends in one of done / started / failed, a row in the event
    trail (`update:<provider>`), an immediate re-check of the provider (not for `failed`, nor for "nothing needed
    doing") and, once the last apply of its batch has ended, ONE notification for the whole batch."""

    def __init__(self, store, clock=time.time, executor=None, deliver=None, loop_s=APPLY_LOOP_S):
        self.store = store
        self._clock = clock
        self._execute = executor
        self._deliver = deliver
        self._loop = float(loop_s)
        self._current = None             # the apply in flight
        self._stuck = {}                 # provider id -> the Event of an apply thread that outlived its timeout
        self._batches = {}               # batch id -> what has ended so far, for the one notification
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ the thread

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hub-apply", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout=2.0):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self):
        try:
            self.store.applying_recover()
        except Exception:  # noqa: BLE001
            log.exception("recovering the apply queue failed")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("the apply runner's pass failed")
            self._stop.wait(self._loop)

    def _tool_executor(self):
        if self._execute is not None:
            return self._execute
        from .. import tools
        return lambda name, args: tools.execute_tool(name, args)

    # ------------------------------------------------------------------ one pass

    def tick(self, now=None):
        """One pass: finish or give up on the apply in flight, then start the next queued one.  Returns the provider
        ids it did something for."""
        now = self._clock() if now is None else now
        acted = []
        cur = self._current
        if cur is not None:
            if cur["done"].is_set():
                self._current = None
                self._finish(cur, now)
                acted.append(cur["provider"])
            elif now - cur["started"] > cur["timeout"]:
                self._current = None
                self._give_up(cur, now)
                acted.append(cur["provider"])
                return acted
            else:
                return acted                    # one apply at a time
        for pid, ended in list(self._stuck.items()):
            if ended.is_set():
                del self._stuck[pid]
        row = self.store.applying_claim(now)
        if row is None:
            return acted
        self._begin(row, now)
        acted.append(row["provider"])
        return acted

    def _begin(self, row, now):
        pid, local = row["provider"], row["local_id"]
        check = self.store.update_check_state().get(pid) or {}
        provider = self.store.get_provider(pid)
        tool = check.get("apply_tool")
        reason = None
        if provider is None or not provider["enabled"]:
            reason = "the provider was switched off"
        elif not settings_mod.update_settings(provider.get("settings"), check.get("start_interval_h"))["update_checks"]:
            reason = "update checks were switched off for this provider"
        elif not tool or check.get("apply_problem"):
            reason = "this provider can't apply updates by itself"
        elif pid in self._stuck:
            reason = "an earlier apply for this provider is still running; try again when it has ended"
        if reason:
            self._record(row, "failed", reason, now)
            return
        try:
            timeout = float(check.get("apply_timeout_s") or APPLY_TIMEOUT_DEFAULT_S)
        except (TypeError, ValueError):
            timeout = APPLY_TIMEOUT_DEFAULT_S
        args = _apply_args(check, local, False)
        run = self._tool_executor()
        cur = {"provider": pid, "local_id": local, "row": row, "started": now, "timeout": timeout,
               "done": threading.Event(), "result": None, "error": None}

        def work():
            try:
                cur["result"] = run(tool, dict(args))
            except BaseException as exc:  # noqa: BLE001 - whatever the tool does, it ends in a recorded failure
                cur["error"] = states.clean_text(exc, 150) or "it raised an error"
            finally:
                cur["done"].set()

        self._current = cur
        self.store.add_event("update:%s" % pid, "info", "apply started: %s" % (row.get("title") or local))
        threading.Thread(target=work, name="hub-apply-%s" % pid, daemon=True).start()

    def _finish(self, cur, now):
        row = cur["row"]
        if cur["error"]:
            self._record(row, "failed", "the apply failed: %s" % cur["error"], now)
            return
        parsed = parse_apply_result(cur["result"])
        self._record(row, parsed["state"], parsed["message"], now, no_change=parsed["no_change"])

    def _give_up(self, cur, now):
        self._stuck[cur["provider"]] = cur["done"]
        self._record(cur["row"], "failed",
                     "the apply took longer than %d s and was given up on" % int(cur["timeout"]), now)

    # ------------------------------------------------------------------ the record

    def _record(self, row, state, message, now, no_change=False):
        pid, local, title = row["provider"], row["local_id"], row.get("title") or row["local_id"]
        message = states.clean_text(message, MAX_APPLY_MESSAGE) or ""
        try:
            self.store.applying_finish(pid, local, state, message, now=now)
            self.store.add_event("update:%s" % pid, "warn" if state == "failed" else "info",
                                 "apply %s: %s%s" % (state, title, (" - " + message) if message else ""))
            if state in ("done", "started") and not no_change:
                self.store.update_check_put(pid, requested_at=now)      # look again at once; no manual-check limit
        except Exception:  # noqa: BLE001
            log.exception("recording an apply for %s failed", pid)
        batch = row.get("batch") or ""
        tally = self._batches.setdefault(batch or ("single-%s-%s" % (pid, local)), {
            "origin": row.get("origin") or "manual", "done": [], "started": [], "unchanged": [], "failed": []})
        key = "failed" if state == "failed" else ("unchanged" if no_change else state)
        tally[key].append({"title": title, "message": message})
        try:
            pending = self.store.applying_pending(batch) if batch else 0
        except Exception:  # noqa: BLE001
            pending = 0
        if pending == 0:
            self._announce(self._batches.pop(batch or ("single-%s-%s" % (pid, local))))

    def _announce(self, summary):
        try:
            from . import notify as notify_mod
            notify_mod.announce_updates(summary, deliver=self._deliver)
        except Exception:  # noqa: BLE001 - a notification that can't be sent never stops the runner
            log.exception("the update-apply notification failed")
