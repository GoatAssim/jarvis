"""TOOL_DOWNLOADS: a tool file declares that it supplies downloads of its own (hub plan 5.2).

    TOOL_DOWNLOADS = [
        {
            "id": "model_fetch",              # required, lower_snake, unique across Jarvis
            "label": "Model downloads",       # required, shown as the provider badge / filter
            "mode": "push",                   # required: "push" | "poll" | "both"
            "kind": "file",                   # default kind of its rows: video audio file torrent game other
            "icon": "M",                      # optional, 1-4 characters
            "poll_tool": "model_fetch_list",  # required for poll / both: a read-only tool of THIS file
            "interval_s": 3,                  # poll cadence
            "update_tool": "check_model_updates",   # optional, any mode: a read-only tool of THIS file that
                                              # answers {"updates": [...], "complete": bool}  (N23)
            "update_interval_h": 24,          # optional: starting hours between two checks (0.5 to 720)
            "update_group": "packages",       # optional: mods | games | packages | apps | other (default other)
            "update_args": {"manager": "pip"},   # optional: arguments of every update_tool call (at most 512 bytes of JSON)
            "apply_tool": "apply_model_update",  # optional: a CONFIRM-GATED tool of THIS file that applies ONE finding
            "apply_args": {},                 # optional: arguments merged into every apply call (never `id` / `dry_run`)
            "apply_timeout_s": 600,           # optional: 30 to 3600 seconds per apply call (default 600)
            "updates_only": False,            # optional: True = "push" provider that only reports / applies updates
            "capabilities": ["pause", "resume", "cancel", "retry", "open", "reveal"],
            "controls": {"pause": "model_fetch_pause", ...},    # capability -> a tool of THIS file
            "open_roots": ["D:/Models"],      # open / reveal / play only inside these folders
            "settings": {"notify_on_done": True},               # starting values the owner then owns
        },
    ]

Same rule as TOOL_UI / DAEMONS / TOOL_COMMANDS: a bad entry is logged and dropped, the file's
tools still load.  This module only VALIDATES (and is import-light); registering the providers
in the hub store is hub/providers.py's job, done by the hub daemon.

What the tool controls (id, label, mode, capabilities, controls, roots, poll tool) is refreshed
whenever the file changes; what the owner may change (enabled, polling interval, settings) is
written once and never overwritten (hub/store.py register_provider).

An `update_tool` (N23) lets a provider say "an update is available" without it being a download: the hub's
update runner calls it on its own thread (never in the poll), at start-up plus a few minutes and then every
`update_interval_h`, and keeps the answer as a list of findings (hub/updates.py).  It follows the SAME load
rules as `poll_tool`: it must be a tool of this file, must not be confirm-gated, must have a name the risk
policy scores as a read (get_ / list_ / check_), and must not be a control tool; it also must not be the
`poll_tool` (the two answer in different shapes, and a poll is given 5 seconds while a check may take 60).
`update_interval_h` is only a starting value, written once: the owner owns it afterwards.  Valid in any mode.

Applying updates (the Updates rework, contract v1).  Four more keys let a provider say how an update is APPLIED, and the
hub's own runner (hub/updates.py `ApplyRunner`) calls it; the hub itself never builds an install command, a shell string or a
package-manager call: only the provider's own apply tool changes the machine.

  update_group      which group the panel puts the provider's findings in and toggles them by: mods, games, packages,
                    apps or other (default other).  Valid only with an `update_tool`.
  update_args       an object (at most 512 bytes of JSON) passed as the arguments of EVERY call to `update_tool`, so
                    several providers can share one tool ({"manager": "winget"}).  Valid only with an `update_tool`.
  apply_tool        a tool of THIS file that applies ONE finding.  Load rules: it is a tool of this file; it is listed in
                    the file's TOOL_CONFIRM_REQUIRED (the model and chat still have to ask first; the owner's click or the
                    owner's standing auto-update tick is the consent for the hub's own call); it is not the poll, the
                    update or a control tool; and, when the loader hands over the schemas, its schema accepts `id`
                    (string) and `dry_run` (boolean).  Valid only with an `update_tool`.
  apply_args        an object (at most 512 bytes) merged into every apply call.  It may not name `id` or `dry_run`: those
                    two are always the hub's (a finding's id exactly as the source gave it, and whether it is a trial run).
  apply_timeout_s   seconds the hub waits for ONE apply call (30 to 3600, default 600).  Valid only with an `apply_tool`.
  updates_only      True = a provider that only reports and applies updates and supplies no downloads.  It must be
                    `"mode": "push"` with an `update_tool`; the panels do not list it as a download source.

An apply tool answers {"ok": bool, "state": "done" | "started" | "unchanged" | "failed", "message": "<one sentence>",
"plan": "<dry_run only>", "installed": "<optional>", "needs_restart": <optional bool>, "error": "<optional>"}.  With
`dry_run: true` it changes NOTHING and fills `plan`.  The same bad-entry rule applies: a bad entry is logged and dropped,
the file's tools still load.  The same goes for a tool the owner made in ~/.jarvis/tools/ (same loader, same validator).

Trust: declaring a provider is the owner's code on the owner's machine, the same trust as
TOOL_UI and DAEMONS.  There is no model-facing way to create one.  A `poll_tool` must be
read-only: it is rejected when it is listed in TOOL_CONFIRM_REQUIRED or when the risk policy
scores its name above a read (so a tool named `*_delete` / `*_install` / `*_run` can't be a poll
tool), because the hub calls it unattended every few seconds.
"""

import os
from pathlib import Path

MAX_PER_FILE = 8
MAX_SETTINGS_BYTES = 4096
ALLOWED_KEYS = frozenset(("id", "label", "mode", "kind", "icon", "poll_tool", "interval_s", "capabilities",
                          "controls", "open_roots", "settings", "update_tool", "update_interval_h",
                          "update_group", "update_args", "apply_tool", "apply_args", "apply_timeout_s",
                          "updates_only"))
# The groups the Updates panel sorts findings into (contract item 1).  hub/settings.py repeats them for the
# `updates.include_groups` setting; tests/test_hub_updates_validate.py checks the two lists agree.
UPDATE_GROUPS = ("mods", "games", "packages", "apps", "other")
DEFAULT_UPDATE_GROUP = "other"
MAX_ARGS_BYTES = 512
APPLY_TIMEOUT_MIN_S = 30
APPLY_TIMEOUT_MAX_S = 3600
APPLY_TIMEOUT_DEFAULT_S = 600
# What an apply tool's schema must accept (checked when the loader hands the schemas over; hub/updates.py checks
# it again on the runner's side, so a tool file that changes later is never trusted blindly).
APPLY_REQUIRED_PARAMS = {"id": "string", "dry_run": "boolean"}
MAX_READ_RISK = 10          # policy._base_risk of a get_/list_/search_/read_/check_ name is 5


def _states():
    from .hub import states
    return states


def _hours():
    from .hub import settings
    return settings


def _read_risk(name):
    try:
        from . import policy
        return policy._base_risk(name)
    except Exception:  # noqa: BLE001 - if the policy can't be asked, refuse rather than guess
        return 10 ** 6


def _args_problem(value, label):
    """None when `value` is an object of at most MAX_ARGS_BYTES of plain JSON, else a sentence."""
    if not isinstance(value, dict):
        return "'%s' must be an object" % label
    try:
        import json
        if len(json.dumps(value)) > MAX_ARGS_BYTES:
            return "'%s' must be at most %d bytes of JSON" % (label, MAX_ARGS_BYTES)
    except (TypeError, ValueError):
        return "'%s' must be plain JSON values" % label
    return None


def apply_schema_problem(tool_name, schemas):
    """None when the schema of `tool_name` accepts `id` (string) and `dry_run` (boolean), else one sentence.

    `schemas` is a list of tool schema dicts ({"name", "parameters": {"properties": {...}}}) or a dict name -> schema.
    A tool with no schema in the list is a problem (the contract needs both parameters declared); the caller decides
    whether it has the list at all (validate_downloads skips this check when `schemas` is None)."""
    found = None
    items = schemas.values() if isinstance(schemas, dict) else (schemas or ())
    for schema in items:
        if isinstance(schema, dict) and schema.get("name") == tool_name:
            found = schema
            break
    if found is None:
        return "its schema was not found"
    params = found.get("parameters")
    props = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(props, dict):
        return "its schema declares no parameters"
    for name, kind in APPLY_REQUIRED_PARAMS.items():
        spec = props.get(name)
        if not isinstance(spec, dict) or spec.get("type") != kind:
            return "its schema must accept '%s' (%s)" % (name, kind)
    return None


def validate_downloads(raw, filename, tool_names, confirm_required=(), schemas=None):
    """Validate a file's TOOL_DOWNLOADS.  Returns (entries, errors); a bad entry is dropped.

    `schemas` (optional) is the file's TOOL_SCHEMAS; when given, an `apply_tool`'s schema is checked for `id` and
    `dry_run` right here.  tool_loader.py does not pass it yet (see the Updates core report, "For the merge"), so the
    same check also runs in hub/updates.py before a provider is allowed to apply anything."""
    st = _states()
    errors, entries = [], []
    if not isinstance(raw, (list, tuple)):
        return [], ["%s: TOOL_DOWNLOADS must be a list of dicts" % filename]
    if len(raw) > MAX_PER_FILE:
        errors.append("%s: TOOL_DOWNLOADS lists %d entries; only the first %d are used"
                      % (filename, len(raw), MAX_PER_FILE))
        raw = list(raw)[:MAX_PER_FILE]
    names = set(tool_names or ())
    confirm = set(confirm_required or ())
    seen = set()
    for index, item in enumerate(raw):
        where = "%s: TOOL_DOWNLOADS[%d]" % (filename, index)
        if not isinstance(item, dict):
            errors.append("%s must be a dict - dropped" % where)
            continue
        unknown = sorted(set(item) - ALLOWED_KEYS)
        if unknown:
            errors.append("%s has unknown key(s) %s - dropped" % (where, unknown))
            continue
        pid = item.get("id")
        if not isinstance(pid, str) or not st.PROVIDER_ID_RE.match(pid):
            errors.append("%s needs an 'id' in lower_snake (a-z, 0-9, _; at most 32) - dropped" % where)
            continue
        if pid in st.RESERVED_PROVIDERS:
            errors.append("%s: id %r is reserved for the hub's own engines - dropped" % (where, pid))
            continue
        if pid in seen:
            errors.append("%s: id %r is used twice - dropped" % (where, pid))
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append("%s needs a 'label' - dropped" % where)
            continue
        mode = item.get("mode")
        if mode not in ("push", "poll", "both"):
            errors.append("%s: 'mode' must be push, poll or both - dropped" % where)
            continue
        kind = item.get("kind", "file")
        if kind not in st.KINDS:
            errors.append("%s: 'kind' must be one of %s - dropped" % (where, ", ".join(st.KINDS)))
            continue
        icon = item.get("icon", "")
        if not isinstance(icon, str) or len(icon) > 4:
            errors.append("%s: 'icon' must be 1-4 characters - dropped" % where)
            continue

        caps_raw = item.get("capabilities", [])
        if not isinstance(caps_raw, (list, tuple)) or any(c not in st.CAPABILITIES for c in caps_raw):
            errors.append("%s: 'capabilities' must be a list drawn from %s - dropped"
                          % (where, ", ".join(st.CAPABILITIES)))
            continue
        caps = st.clean_capabilities(caps_raw)

        controls_raw = item.get("controls", {})
        problem = None
        if not isinstance(controls_raw, dict):
            problem = "'controls' must be a dict of capability -> tool"
        else:
            for cap, tool in controls_raw.items():
                if cap not in caps:
                    problem = "'controls' names %r, which is not in 'capabilities'" % (cap,)
                elif not isinstance(tool, str) or tool not in names:
                    problem = "control %r names %r, which is not a tool of this file" % (cap, tool)
                if problem:
                    break
        if problem:
            errors.append("%s: %s - dropped" % (where, problem))
            continue

        poll_tool = item.get("poll_tool")
        if mode in ("poll", "both"):
            if not isinstance(poll_tool, str) or poll_tool not in names:
                errors.append("%s: mode %r needs a 'poll_tool' that is a tool of this file - dropped" % (where, mode))
                continue
            if poll_tool in confirm or _read_risk(poll_tool) > MAX_READ_RISK:
                errors.append("%s: poll_tool %r must be a read-only tool (not confirm-gated, and a name the risk "
                              "policy scores as a read, like get_/list_/check_) - dropped" % (where, poll_tool))
                continue
            if poll_tool in (controls_raw or {}).values():
                errors.append("%s: poll_tool %r is also a control tool - dropped" % (where, poll_tool))
                continue
        elif poll_tool is not None:
            errors.append("%s: 'poll_tool' is only used with mode poll or both - dropped" % where)
            continue

        update_tool = item.get("update_tool")
        update_hours = item.get("update_interval_h")
        if update_tool is not None:
            if not isinstance(update_tool, str) or update_tool not in names:
                errors.append("%s: 'update_tool' %r must be a tool of this file - dropped" % (where, update_tool))
                continue
            if update_tool in confirm or _read_risk(update_tool) > MAX_READ_RISK:
                errors.append("%s: update_tool %r must be a read-only tool (not confirm-gated, and a name the risk "
                              "policy scores as a read, like get_/list_/check_) - dropped" % (where, update_tool))
                continue
            if update_tool in (controls_raw or {}).values():
                errors.append("%s: update_tool %r is also a control tool - dropped" % (where, update_tool))
                continue
            if update_tool == poll_tool:
                errors.append("%s: update_tool %r is also the poll_tool - dropped" % (where, update_tool))
                continue
            if update_hours is not None:
                hours, bad = _hours().UPDATE_SETTINGS["update_interval_h"][1](update_hours)
                if bad:
                    errors.append("%s: 'update_interval_h' must be a number of hours from %g to %g - dropped"
                                  % (where, _hours().UPDATE_INTERVAL_H_MIN, _hours().UPDATE_INTERVAL_H_MAX))
                    continue
                update_hours = hours
        elif update_hours is not None:
            errors.append("%s: 'update_interval_h' is only used with an 'update_tool' - dropped" % where)
            continue

        group = item.get("update_group", DEFAULT_UPDATE_GROUP)
        update_args = item.get("update_args")
        apply_tool = item.get("apply_tool")
        apply_args = item.get("apply_args")
        apply_timeout = item.get("apply_timeout_s")
        updates_only = item.get("updates_only", False)
        problem = None
        if group not in UPDATE_GROUPS:
            problem = "'update_group' must be one of %s" % ", ".join(UPDATE_GROUPS)
        elif "update_group" in item and update_tool is None:
            problem = "'update_group' is only used with an 'update_tool'"
        elif update_args is not None and update_tool is None:
            problem = "'update_args' is only used with an 'update_tool'"
        elif update_args is not None and _args_problem(update_args, "update_args"):
            problem = _args_problem(update_args, "update_args")
        elif apply_tool is not None and update_tool is None:
            problem = "'apply_tool' is only used with an 'update_tool'"
        elif apply_tool is None and (apply_args is not None or apply_timeout is not None):
            problem = "'apply_args' and 'apply_timeout_s' are only used with an 'apply_tool'"
        elif not isinstance(updates_only, bool):
            problem = "'updates_only' must be true or false"
        elif updates_only and (update_tool is None or mode != "push"):
            problem = "'updates_only' needs mode 'push' and an 'update_tool'"
        elif apply_tool is not None:
            if not isinstance(apply_tool, str) or apply_tool not in names:
                problem = "'apply_tool' %r must be a tool of this file" % (apply_tool,)
            elif apply_tool not in confirm:
                problem = ("apply_tool %r must be listed in this file's TOOL_CONFIRM_REQUIRED (an apply changes the "
                           "machine, so the model and chat must ask first)" % apply_tool)
            elif apply_tool in (controls_raw or {}).values():
                problem = "apply_tool %r is also a control tool" % apply_tool
            elif apply_tool in (poll_tool, update_tool):
                problem = "apply_tool %r is also the poll_tool or the update_tool" % apply_tool
            elif apply_args is not None and _args_problem(apply_args, "apply_args"):
                problem = _args_problem(apply_args, "apply_args")
            elif isinstance(apply_args, dict) and set(apply_args) & {"id", "dry_run"}:
                problem = "'apply_args' may not name 'id' or 'dry_run' (the hub sets those two)"
            elif apply_timeout is not None and (
                    isinstance(apply_timeout, bool) or not isinstance(apply_timeout, (int, float))
                    or not APPLY_TIMEOUT_MIN_S <= float(apply_timeout) <= APPLY_TIMEOUT_MAX_S):
                problem = "'apply_timeout_s' must be a number from %d to %d" % (APPLY_TIMEOUT_MIN_S, APPLY_TIMEOUT_MAX_S)
            elif schemas is not None and apply_schema_problem(apply_tool, schemas):
                problem = "apply_tool %r: %s" % (apply_tool, apply_schema_problem(apply_tool, schemas))
        if problem:
            errors.append("%s: %s - dropped" % (where, problem))
            continue

        interval = item.get("interval_s", 3)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0.5 <= float(interval) <= 3600:
            errors.append("%s: 'interval_s' must be a number from 0.5 to 3600 - dropped" % where)
            continue

        roots_raw = item.get("open_roots", [])
        if not isinstance(roots_raw, (list, tuple)) or any(not isinstance(r, str) or not r.strip() for r in roots_raw):
            errors.append("%s: 'open_roots' must be a list of folder paths - dropped" % where)
            continue
        roots = []
        for r in list(roots_raw)[:8]:
            expanded = os.path.expanduser(r.strip())
            if not (os.path.isabs(expanded) or (len(expanded) > 2 and expanded[1] == ":")):
                problem = "open_roots entry %r must be an absolute path (or start with ~)" % r
                break
            roots.append(str(Path(expanded)))
        else:
            problem = None
        if problem:
            errors.append("%s: %s - dropped" % (where, problem))
            continue

        settings = item.get("settings", {})
        if not isinstance(settings, dict):
            errors.append("%s: 'settings' must be a dict - dropped" % where)
            continue
        try:
            import json
            if len(json.dumps(settings)) > MAX_SETTINGS_BYTES:
                raise ValueError("too large")
        except (TypeError, ValueError):
            errors.append("%s: 'settings' must be small plain JSON values - dropped" % where)
            continue

        seen.add(pid)
        entries.append({
            "id": pid, "label": label.strip()[:60], "mode": mode, "kind": kind, "icon": icon,
            "poll_tool": poll_tool if mode in ("poll", "both") else None, "interval_s": float(interval),
            "capabilities": caps, "controls": {c: t for c, t in (controls_raw or {}).items()},
            "open_roots": roots, "settings": dict(settings), "source_file": filename,
            "update_tool": update_tool, "update_interval_h": update_hours if update_tool else None,
            "update_group": group if update_tool else DEFAULT_UPDATE_GROUP,
            "update_args": dict(update_args) if update_tool and isinstance(update_args, dict) else {},
            "apply_tool": apply_tool,
            "apply_args": dict(apply_args) if apply_tool and isinstance(apply_args, dict) else {},
            "apply_timeout_s": (float(apply_timeout) if apply_timeout is not None else float(APPLY_TIMEOUT_DEFAULT_S))
            if apply_tool else None,
            "updates_only": bool(updates_only),
        })
    return entries, errors
