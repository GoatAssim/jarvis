"""The admin side of the Download Hub's update findings (N23): plain functions, the same shape as the ops of
`hub_admin.py` (one request dict in, one JSON-able dict out, `{"ok": False, "error": ...}` for a refusal, never
a raise).  `hub_admin.py` and `web/server.js` belong to another agent this round, so this file only EXPORTS the
ops (it sits in hub/ next to updates.py); the hookup lines are in docs/reports/UPDATES_CORE.md ("For the merge").

  updates(request)           READ.   {"provider": "<id>", "include_hidden": false}
                             -> the findings, each provider's health in words, the settings the Updates tab
                             edits (update_checks, update_interval_h) and whether the daemon is running.
  updates_check(request)     WRITE.  {"provider": "<id>"} (none = every provider that has an update check):
                             sets the flag the daemon's runner picks up.  Nothing is started here.
  updates_dismiss(request)   WRITE.  {"provider": "<id>", "id": "<finding id>"}: "Ignore this version".

The two writes are the owner's own button presses: an unattended process (a chat reply, a scheduled job, a
sub-agent) is refused, the same test as hub_admin.unattended_reason().  No model-facing tool reaches this file
(the chat tools are in actions/download_updates.py and go through hub/updates.py directly).  Nothing here
installs, upgrades or removes anything, and no file is touched.
"""

import os
import re

READ_OPS = ("updates",)
WRITE_OPS = ("updates_check", "updates_dismiss")
OPS = READ_OPS + WRITE_OPS

_UNATTENDED_VARS = ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID")
_UNATTENDED_MESSAGE = "The Download Hub's update controls can only be used from the Jarvis page itself."
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def _err(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def unattended_reason():
    """Same variables and same answer as hub_admin.unattended_reason(): a chat reply, a scheduled job and a
    sub-agent all run the same CLI, and these writes are the owner's own button presses."""
    for var in _UNATTENDED_VARS:
        if os.environ.get(var):
            return _UNATTENDED_MESSAGE
    if os.environ.get("JARVIS_CONTEXT") in ("scheduled", "unattended"):
        return _UNATTENDED_MESSAGE
    return None


def _store(store):
    from .store import Store              # lazy: nothing is loaded until a request arrives
    return store or Store()


def _provider(request):
    """A well-formed provider id from the request, '' when there is none, None when it is malformed."""
    raw = str((request or {}).get("provider") or "").strip()
    if not raw:
        return ""
    return raw if _PROVIDER_RE.match(raw) else None


def provider_update_settings(store=None):
    """READ: the two update settings of every provider the Settings tab should show them for, with the limits.

    A provider is listed when it has an update check, or when its update checks are switched OFF (the Updates tab
    leaves a provider whose checks are off out completely, so this list is the owner's only way back to the switch).
    Switched-off providers are listed too.  Never raises: {"providers": [], "limits": {...}} on any trouble."""
    from . import settings as settings_mod
    out = {"providers": [], "limits": {"interval_h_min": settings_mod.UPDATE_INTERVAL_H_MIN,
                                       "interval_h_max": settings_mod.UPDATE_INTERVAL_H_MAX,
                                       "interval_h_default": settings_mod.UPDATE_INTERVAL_H_DEFAULT}}
    try:
        store = _store(store)
        checks = store.update_check_state()
        for p in store.list_providers():
            row = checks.get(p["id"]) or {}
            cfg = settings_mod.update_settings(p.get("settings"), row.get("start_interval_h"))
            has_check = bool(row.get("update_tool"))
            if not has_check and cfg["update_checks"]:
                continue
            out["providers"].append({"provider": p["id"], "label": str(p.get("label") or p["id"])[:60],
                                     "has_check": has_check, "update_checks": bool(cfg["update_checks"]),
                                     "update_interval_h": cfg["update_interval_h"]})
    except Exception:  # noqa: BLE001 - the Settings tab must still open
        out["providers"] = []
    return out


def updates(request=None, store=None):
    """READ: the Updates tab's data.  Never raises."""
    try:
        from . import settings as settings_mod, updates as updates_mod
        request = request if isinstance(request, dict) else {}
        provider = _provider(request)
        if provider is None:
            return _err("that isn't a provider id")
        store = _store(store)
        out = updates_mod.present(store, provider=provider or None,
                                  include_hidden=request.get("include_hidden") is True)
        out["limits"] = {"manual_check_s": updates_mod.MANUAL_LIMIT_S, "check_timeout_s": updates_mod.CHECK_TIMEOUT_S,
                         "interval_h_min": settings_mod.UPDATE_INTERVAL_H_MIN,
                         "interval_h_max": settings_mod.UPDATE_INTERVAL_H_MAX,
                         "interval_h_default": settings_mod.UPDATE_INTERVAL_H_DEFAULT}
        return out
    except Exception as exc:  # noqa: BLE001 - a panel call must answer, not crash the page
        return _err("Download Hub error: %s" % str(exc)[:160])


def updates_check(request=None, store=None):
    """WRITE: ask for a check now.  Refused for an unattended process."""
    why = unattended_reason()
    if why:
        return _err(why, code="unattended")
    try:
        from . import updates as updates_mod
        request = request if isinstance(request, dict) else {}
        provider = _provider(request)
        if provider is None:
            return _err("that isn't a provider id")
        return updates_mod.request_check(_store(store), provider=provider or None)
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't start the check: %s" % str(exc)[:160])


def updates_dismiss(request=None, store=None):
    """WRITE: "Ignore this version" for one finding.  Hides it until the source reports a different
    `available` text; deletes nothing.  Refused for an unattended process."""
    why = unattended_reason()
    if why:
        return _err(why, code="unattended")
    try:
        from . import updates as updates_mod
        request = request if isinstance(request, dict) else {}
        provider = _provider(request)
        local = str(request.get("id") or "").strip()
        if not provider or not local or not _ID_RE.match(local):
            return _err("say which update: a provider and an id")
        return updates_mod.dismiss_finding(_store(store), provider, local)
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't ignore that: %s" % str(exc)[:160])
