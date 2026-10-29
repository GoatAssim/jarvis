"""The admin side of the Download Hub's update findings (N23, and the Updates rework): plain functions, the same shape as
the ops of `hub_admin.py` (one request dict in, one JSON-able dict out, `{"ok": False, "error": ...}` for a refusal,
never a raise).  `hub_admin.py` registers them (READ_OPS / WRITE_OPS below are what it reads) and `web/server.js` routes
them.

  updates(request)           READ.   {"provider": "<id>", "include_hidden": false}
                             -> the findings (each with its group, `can_apply` and the state of its apply), each
                             provider's health in words, the groups, the update settings (`settings`: auto_update,
                             include_groups, auto_max_per_cycle), how many applies are queued / running and whether the
                             daemon is running.
  updates_check(request)     WRITE.  {"provider": "<id>"} (none = every provider that has an update check):
                             sets the flag the daemon's runner picks up.  Nothing is started here.
  updates_dismiss(request)   WRITE.  {"provider": "<id>", "id": "<finding id>"}: "Ignore this version".
  updates_apply(request)     WRITE.  {"provider": "<id>", "id": "<finding id>"} or {"all": true, "group": "<g>",
                             "provider": "<id>"}: queues the findings for the daemon's apply runner.  Refused when the
                             daemon isn't running.  Needs no auto-update switch: it is the owner's click.
  updates_dry_run(request)   WRITE.  {"provider": "<id>", "id": "<finding id>"}: asks the provider's apply tool for a
                             PLAN (`dry_run: true`, it changes nothing) and returns it.  It starts the provider's tool,
                             so it is refused for an unattended process like the other writes.
  updates_set(request)       WRITE.  {"scope": "global" | "group" | "provider", "id": "<group or provider>",
                             "key": "<setting>", "value": <value>}: global keys auto_update, include_groups,
                             auto_max_per_cycle; group keys included (add / remove the group from include_groups),
                             update_checks, auto_update (every provider of the group); provider keys update_checks,
                             update_interval_h, auto_update.  Every value goes through hub/settings.py's validators.

The writes are the owner's own button presses: an unattended process (a chat reply, a scheduled job, a sub-agent) is
refused, the same test as hub_admin.unattended_reason().  The model's own tools (actions/download_updates.py) go through
hub/updates.py directly and are confirm-gated; none of them can switch automatic updating on.  This file never installs,
upgrades or removes anything itself: applying is the provider's own apply tool, run by the daemon (or, for a plan, here).
"""

import os
import re

READ_OPS = ("updates",)
WRITE_OPS = ("updates_check", "updates_dismiss", "updates_apply", "updates_dry_run", "updates_set")
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


def updates_apply(request=None, store=None):
    """WRITE: queue one finding, or every applicable one (optionally of one provider / group), to be applied.
    Refused for an unattended process, and when the daemon isn't running."""
    why = unattended_reason()
    if why:
        return _err(why, code="unattended")
    try:
        from . import settings as settings_mod, updates as updates_mod
        request = request if isinstance(request, dict) else {}
        provider = _provider(request)
        if provider is None:
            return _err("that isn't a provider id")
        local = str(request.get("id") or "").strip()
        if local and not _ID_RE.match(local):
            return _err("that isn't an update id")
        group = request.get("group")
        if group not in (None, "") and group not in settings_mod.UPDATE_GROUPS:
            return _err("unknown group; use one of %s" % ", ".join(settings_mod.UPDATE_GROUPS))
        return updates_mod.request_apply(_store(store), provider=provider or None, local_id=local or None,
                                         all_updates=request.get("all") is True, group=group or None)
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't queue that: %s" % str(exc)[:160])


def updates_dry_run(request=None, store=None):
    """WRITE (in effect a read): the plan the provider's apply tool gives for one finding.  Refused for an unattended
    process: it starts the provider's own tool."""
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
        return updates_mod.dry_run(_store(store), provider, local)
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't get a plan: %s" % str(exc)[:160])


_GLOBAL_KEYS = ("auto_update", "include_groups", "auto_max_per_cycle")
_PROVIDER_KEYS = ("update_checks", "update_interval_h", "auto_update")
_GROUP_KEYS = ("included", "update_checks", "auto_update")


def updates_set(request=None, store=None):
    """WRITE: one Updates setting, checked by hub/settings.py's validators.  Refused for an unattended process.

    Returns {"ok", "settings"} (the global block as it is now) plus `changed`: the provider ids touched for scope
    group / provider."""
    why = unattended_reason()
    if why:
        return _err(why, code="unattended")
    try:
        from . import settings as settings_mod
        request = request if isinstance(request, dict) else {}
        scope = request.get("scope")
        key = request.get("key")
        value = request.get("value")
        store = _store(store)
        changed = []
        if scope == "global":
            if key not in _GLOBAL_KEYS:
                return _err("the global update settings are %s" % ", ".join(_GLOBAL_KEYS))
            clean, errors = settings_mod.validate_update({"updates." + key: value}, allowed=settings_mod.UPDATES_WRITABLE)
            if errors:
                return _err("; ".join(errors))
            store.set_settings(clean)
        elif scope == "group":
            group = str(request.get("id") or "")
            if group not in settings_mod.UPDATE_GROUPS:
                return _err("unknown group; use one of %s" % ", ".join(settings_mod.UPDATE_GROUPS))
            if key not in _GROUP_KEYS:
                return _err("the group update settings are %s" % ", ".join(_GROUP_KEYS))
            if key == "included":
                if not isinstance(value, bool):
                    return _err("included must be true or false")
                current = settings_mod.update_global_settings(store.get_settings())["include_groups"]
                wanted = [g for g in settings_mod.UPDATE_GROUPS if (g in current and g != group) or (g == group and value)]
                clean, errors = settings_mod.validate_update({"updates.include_groups": wanted},
                                                             allowed=settings_mod.UPDATES_WRITABLE)
                if errors:
                    return _err("; ".join(errors))
                store.set_settings(clean)
            else:
                checks = store.update_check_state()
                pids = sorted(pid for pid, row in checks.items()
                              if row.get("update_tool") and (row.get("update_group") or "other") == group)
                if not pids:
                    return _err("no provider with an update check is in that group")
                changed = _set_provider_key(store, pids, key, value)
                if isinstance(changed, dict):
                    return changed
        elif scope == "provider":
            pid = _provider({"provider": request.get("id")})
            if not pid:
                return _err("that isn't a provider id")
            if key not in _PROVIDER_KEYS:
                return _err("the provider update settings are %s" % ", ".join(_PROVIDER_KEYS))
            if store.get_provider(pid) is None:
                return _err("no provider called %s" % pid)
            changed = _set_provider_key(store, [pid], key, value)
            if isinstance(changed, dict):
                return changed
        else:
            return _err("scope must be global, group or provider")
        return {"ok": True, "settings": settings_mod.update_global_settings(store.get_settings()),
                "changed": changed}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't change that: %s" % str(exc)[:160])


def _set_provider_key(store, provider_ids, key, value):
    """Validate one provider-level update setting and merge it into each provider.  The provider ids written, or an
    {"ok": False} dict when the value is refused (nothing is written then)."""
    from . import settings as settings_mod
    clean, errors = settings_mod.validate_provider_settings(provider_ids[0], {key: value}, allow_auto_update=True)
    if errors:
        return _err("; ".join(errors))
    done = []
    for pid in provider_ids:
        if store.update_provider(pid, settings=clean) is not None:
            done.append(pid)
    return done
