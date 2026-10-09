"""L.12 - daemons a tool supplies.

A tool file (jarvis/actions/*.py or ~/.jarvis/tools/*.py) that defines tools
can ALSO declare background services of its own:

    DAEMONS = [
        {
            "id": "worker",                  # required; becomes "<file>-worker"
            "name": "My worker",             # optional label
            "description": "Does X.",        # optional, shown in the panel
            "command": ["{python}", "{tool_dir}/worker.py"],   # required
            "cwd": "",                       # default: the file's own folder
            "env": {"MODE": "fast"},         # optional
            "supports_stdin": False,
            "shell": False,
            "autostart": False,              # the PRESET - see below
            "restart": "on-failure",         # never | on-failure | always
            "restart_delay": 5, "max_restarts": 5,
            "stop_signal": "TERM", "stop_timeout": 10,
            "categories": ["My tool"],       # SUGGESTED - see below
            "notes": "",
        },
    ]

`command` is a list (used as-is) or a string (split like `daemon-add`, or kept
verbatim when `shell` is true). `{python}` is the interpreter Jarvis is running
under and `{tool_dir}` the folder the file lives in; both may appear in
`command` and `cwd`. A bad entry is logged and dropped; the tools still load.

What the tool controls and what the owner keeps
-----------------------------------------------
The tool controls WHAT RUNS: command, cwd, env, stdin, shell and the
description. Those are re-applied whenever the file changes and are locked in
the editor (daemons._OWNER_LOCKED). Everything else is a starting point the
OWNER then owns: the autostart preset, the suggested categories, restart
policy, notes - and name, enabled, schedule and the console window, which are
always the owner's. They are written once, when the daemon is first
registered, and never overwritten afterwards, so an edit made in the panel
survives the tool being updated.

Lifecycle (see sync())
----------------------
  installed / loaded   the daemon is registered, `owner` = the file's name.
  file removed         the daemon is stopped and removed.
  entry dropped from   the daemon is stopped and removed.
    DAEMONS
  file present but     the daemon is switched OFF (stopped, enabled=False,
    not loaded           `dormant`): a renamed `.py.disabled` file or one that
    (disabled/broken)    no longer loads. It is switched back on, as it was,
                         when the tool loads again.

Turning the tool's tools off in the Tool Manager does NOT touch its daemons:
that cascade is a separate, still-open decision (master plan Q-L46m).

The registry is reconciled lazily and cheaply: sync_if_stale() compares a
fingerprint of the two tool folders and only does any work when it changed.
"""
import hashlib
import os
import sys
import time
from pathlib import Path

from . import atomic_io
from . import categories as _categories

MAX_PER_FILE = 8

ALLOWED_KEYS = {
    "id", "name", "description", "command", "cwd", "env", "supports_stdin",
    "shell", "autostart", "restart", "restart_delay", "max_restarts",
    "stop_signal", "stop_timeout", "categories", "notes",
}

# Re-applied from the declaration on every sync. Mirrors daemons._OWNER_LOCKED
# (which also lists `shell` / `supports_stdin` / `env` / `cwd` / `description`
# / `argv` by their registry names).
TOOL_CONTROLLED = ("argv", "cwd", "env", "supports_stdin", "shell", "description")

STATE_FILE_NAME = "daemons_sync.json"


def _daemons():
    from . import daemons
    return daemons


def owner_of(filename):
    """The owner id for a tool file name: its stem, in daemon-id form."""
    return _daemons().normalize_id(Path(str(filename)).stem)


def python_executable():
    """`{python}`: the interpreter running Jarvis. A frozen build's own
    executable is Jarvis, not Python, so look one up on PATH instead."""
    import shutil
    if getattr(sys, "frozen", False):
        return shutil.which("python") or shutil.which("py") or "python"
    return sys.executable or "python"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _fill(text, tool_dir):
    return (str(text)
            .replace("{python}", python_executable())
            .replace("{tool_dir}", str(tool_dir)))


def validate_daemons(raw, filename, base_dir):
    """Validate a file's DAEMONS. Returns (entries, errors).

    `entries` are normalised and ready for sync(): every key present, the
    command resolved to an argv, `did` the registry id. A problem with one
    entry drops that entry only.
    """
    d = _daemons()
    errors = []
    entries = []
    owner = owner_of(filename)
    if not owner:
        return [], [f"{filename}: DAEMONS needs a file name with letters or digits"]
    if not isinstance(raw, (list, tuple)):
        return [], [f"{filename}: DAEMONS must be a list of dicts"]
    if len(raw) > MAX_PER_FILE:
        errors.append(f"{filename}: DAEMONS lists {len(raw)} entries; "
                      f"only the first {MAX_PER_FILE} are used")
        raw = list(raw)[:MAX_PER_FILE]
    tool_dir = Path(base_dir) if base_dir else Path(".")
    seen = set()

    for index, item in enumerate(raw):
        where = f"{filename}: DAEMONS[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be a dict - dropped")
            continue
        unknown = sorted(set(item) - ALLOWED_KEYS)
        if unknown:
            errors.append(f"{where} has unknown key(s) {unknown} - dropped")
            continue

        eid = d.normalize_id(item.get("id"))
        if not eid:
            errors.append(f"{where} needs an 'id' with letters or digits - dropped")
            continue
        did = f"{owner}-{eid}"
        if len(did) > 40:
            errors.append(f"{where}: '{did}' is {len(did)} characters; the file "
                          "name and id together can be 40 at most - dropped")
            continue
        if did in seen:
            errors.append(f"{where}: id '{eid}' is used twice - dropped")
            continue

        problem = None
        shell = item.get("shell", False)
        stdin = item.get("supports_stdin", False)
        autostart = item.get("autostart", False)
        for label, value in (("shell", shell), ("supports_stdin", stdin),
                             ("autostart", autostart)):
            if not isinstance(value, bool):
                problem = f"'{label}' must be true or false"
        for label in ("name", "description", "notes", "cwd"):
            if item.get(label) is not None and not isinstance(item.get(label), str):
                problem = f"'{label}' must be text"

        restart = item.get("restart", d.RESTART_NEVER)
        if restart not in d.RESTART_POLICIES:
            problem = "'restart' must be one of: " + ", ".join(d.RESTART_POLICIES)
        signal_name = item.get("stop_signal", "TERM")
        if signal_name not in d.STOP_SIGNALS:
            problem = "'stop_signal' must be one of: " + ", ".join(d.STOP_SIGNALS)
        numbers = {}
        for label, default, floor in (
                ("restart_delay", d.DEFAULT_RESTART_DELAY, 1),
                ("max_restarts", d.DEFAULT_MAX_RESTARTS, 0),
                ("stop_timeout", d.DEFAULT_STOP_TIMEOUT, 1)):
            value = item.get(label, default)
            if not _is_int(value) or value < floor:
                problem = f"'{label}' must be a whole number, {floor} or more"
            numbers[label] = value

        env = item.get("env") or {}
        if not isinstance(env, dict):
            problem = "'env' must be a dict of text to text"
            env = {}
        else:
            for k, v in env.items():
                if (not isinstance(k, str) or not isinstance(v, str)
                        or not k or "=" in k or any(c.isspace() for c in k)):
                    problem = ("'env' needs names without spaces or '=' and "
                               "text values")
                    break

        cats, cat_err = _categories.normalize_list(item.get("categories"), strict=True)
        if cat_err:
            problem = f"categories: {cat_err}"

        command = item.get("command")
        argv = []
        if isinstance(command, str):
            if not command.strip():
                problem = "'command' is required"
            elif shell is True:
                argv = [_fill(command.strip(), tool_dir)]
            else:
                try:
                    argv = [_fill(a, tool_dir) for a in d._split_command(command)]
                except ValueError as exc:
                    problem = f"could not parse 'command': {exc}"
        elif isinstance(command, (list, tuple)) and command and all(isinstance(a, str) for a in command):
            argv = [_fill(a, tool_dir) for a in command]
        else:
            problem = "'command' is required (text, or a list of text)"
        if not problem and (not argv or not any(a.strip() for a in argv)):
            problem = "'command' is required"

        if problem:
            errors.append(f"{where}: {problem} - dropped")
            continue

        cwd = _fill(item.get("cwd") or "", tool_dir).strip()
        if not cwd:
            cwd = str(tool_dir)
        elif not Path(cwd).is_absolute():
            cwd = str((tool_dir / cwd))

        seen.add(did)
        entries.append({
            "did": did, "id": eid, "owner": owner,
            "name": (item.get("name") or "").strip() or eid,
            "description": (item.get("description") or "").strip(),
            "argv": argv, "cwd": cwd, "env": dict(env),
            "supports_stdin": stdin, "shell": shell, "autostart": autostart,
            "restart": restart, "restart_delay": numbers["restart_delay"],
            "max_restarts": numbers["max_restarts"], "stop_signal": signal_name,
            "stop_timeout": numbers["stop_timeout"], "categories": cats,
            "notes": (item.get("notes") or "").strip(),
        })
    return entries, errors


def _new_entry(spec):
    """The registry entry for a daemon registered for the first time. The
    preset fields (autostart, categories, restart policy, notes, name) are
    written here and never touched again."""
    return {
        "id": spec["did"], "name": spec["name"], "builtin": False,
        "owner": spec["owner"],
        "argv": list(spec["argv"]), "cwd": spec["cwd"], "env": dict(spec["env"]),
        "supports_stdin": spec["supports_stdin"],
        "description": spec["description"],
        "categories": list(spec["categories"]),
        "enabled": True, "autostart": spec["autostart"], "next_start": None,
        "shell": spec["shell"], "restart": spec["restart"],
        "restart_delay": spec["restart_delay"],
        "max_restarts": spec["max_restarts"], "stop_signal": spec["stop_signal"],
        "stop_timeout": spec["stop_timeout"], "notes": spec["notes"],
        "console_window_auto": False, "console_window_stop_on_close": False,
    }


def _stop_if_up(did):
    d = _daemons()
    try:
        now = d.status(did)
        if now.get("running") or now.get("supervisor_pid"):
            d.stop(did)
    except Exception:  # noqa: BLE001 - never let a stop failure block the sync
        pass


def sync(records, present_owners):
    """Make the registry's tool-supplied daemons match `records`.

    records          ActionModuleRecord list (tool_loader); only valid ones count
    present_owners   owner ids of every tool file that still exists on disk,
                     loaded or not (a `.py.disabled` or a broken file counts)

    Returns {"added", "updated", "removed", "dormant", "woken", "conflicts"},
    each a list of daemon ids (conflicts: messages). Writes the registry only
    when something changed.
    """
    d = _daemons()
    merged = d._load_registry()
    result = {"added": [], "updated": [], "removed": [], "dormant": [],
              "woken": [], "conflicts": []}
    changed = False

    want = {}
    loaded_owners = set()
    for rec in records or []:
        if not getattr(rec, "valid", False):
            continue
        owner = owner_of(rec.file)
        loaded_owners.add(owner)
        for spec in getattr(rec, "daemons", None) or []:
            if spec["did"] in want:
                result["conflicts"].append(
                    f"'{spec['did']}' is declared by two tool files - "
                    f"keeping {want[spec['did']]['owner']}'s")
                continue
            want[spec["did"]] = spec

    for did, spec in want.items():
        current = merged.get(did)
        if current is None:
            merged[did] = _new_entry(spec)
            result["added"].append(did)
            changed = True
            continue
        if current.get("owner") != spec["owner"]:
            what = ("a built-in daemon" if did in d.BUILTINS else
                    "a daemon of yours" if not current.get("owner") else
                    f"a daemon from '{current.get('owner')}'")
            result["conflicts"].append(
                f"'{did}' (from {spec['owner']}) is already {what} - not registered")
            continue
        touched = False
        for key in TOOL_CONTROLLED:
            if current.get(key) != spec[key]:
                current[key] = spec[key]
                touched = True
        if current.get("dormant"):
            current["enabled"] = bool(current.pop("enabled_before_dormant", True))
            current.pop("dormant", None)
            result["woken"].append(did)
            touched = True
        if touched:
            merged[did] = current
            if did not in result["woken"]:
                result["updated"].append(did)
            changed = True

    for did, entry in list(merged.items()):
        owner = entry.get("owner")
        if not owner or did in want:
            continue
        if owner not in present_owners or owner in loaded_owners:
            # Uninstalled, or the file no longer declares it: gone.
            _stop_if_up(did)
            del merged[did]
            result["removed"].append(did)
            changed = True
        elif not entry.get("dormant"):
            # The file is there but not loading (disabled / broken): off, and
            # remembered, so turning the tool back on turns this back on.
            _stop_if_up(did)
            entry["enabled_before_dormant"] = bool(entry.get("enabled", True))
            entry["enabled"] = False
            entry["dormant"] = True
            merged[did] = entry
            result["dormant"].append(did)
            changed = True

    if changed:
        d._save_registry(merged)
    return result


# ---------------------------------------------------------------------------
# cheap staleness check
# ---------------------------------------------------------------------------

def _state_path():
    return _daemons().JARVIS_DIR / STATE_FILE_NAME


def scan_tool_folders():
    """(fingerprint, present_owner_ids) for the two tool folders. Only names,
    sizes and mtimes are read, so this is cheap enough to run on every
    Daemons-panel poll."""
    from . import custom_tools_store, tool_loader
    present = set()
    rows = []
    for label, folder in (("actions", tool_loader.ACTIONS_DIR),
                          ("user", custom_tools_store.TOOLS_DIR)):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if name.startswith("_"):
                continue
            if name.endswith(".py"):
                stem = name[:-3]
            elif label == "user" and name.endswith(".py.disabled"):
                stem = name[:-len(".py.disabled")]
            else:
                continue
            try:
                st = os.stat(os.path.join(folder, name))
            except OSError:
                continue
            rows.append(f"{label}/{name}:{st.st_size}:{st.st_mtime_ns}")
            owner = _daemons().normalize_id(stem)
            if owner:
                present.add(owner)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(), present


def sync_if_stale(force=False, records=None, scan=None):
    """Reconcile only when the tool folders changed since the last run.

    Returns the sync() result, or None when nothing was stale (or the tools
    could not be loaded - that is remembered per fingerprint, so a broken
    state is retried only when a file changes, not on every poll).
    `records` / `scan` let a caller (or a test) supply what discovery and the
    folder scan would have produced.
    """
    stamp, present = scan if scan is not None else scan_tool_folders()
    state_file = _state_path()
    state = atomic_io.read_json(state_file, default={}, expect=dict)
    if not force and state.get("stamp") == stamp:
        return None
    try:
        if records is None:
            from . import tools
            records = list(getattr(tools, "_AUTO_RECORDS", []))
        result = sync(records, present)
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        atomic_io.write_json(state_file, {"stamp": stamp, "at": time.time(),
                                          "error": str(exc)})
        return None
    atomic_io.write_json(state_file, {"stamp": stamp, "at": time.time(),
                                      "last": result})
    return result
