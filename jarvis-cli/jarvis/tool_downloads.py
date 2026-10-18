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
                          "controls", "open_roots", "settings"))
MAX_READ_RISK = 10          # policy._base_risk of a get_/list_/search_/read_/check_ name is 5


def _states():
    from .hub import states
    return states


def _read_risk(name):
    try:
        from . import policy
        return policy._base_risk(name)
    except Exception:  # noqa: BLE001 - if the policy can't be asked, refuse rather than guess
        return 10 ** 6


def validate_downloads(raw, filename, tool_names, confirm_required=()):
    """Validate a file's TOOL_DOWNLOADS.  Returns (entries, errors); a bad entry is dropped."""
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
        })
    return entries, errors
