"""The Download Hub demo provider's pause / resume / cancel tools (brief 13, part A).

Three small tools the demo provider names in its `controls` (hub/demo.py `CONTROLS`), so the
Pause / Resume / Cancel buttons on the demo rows have something to run.  Each takes the demo
row's own id (`climb`, not `demo:climb`) and calls `hub.demo.control`; every rule lives there.

They live in their own file, not in download_hub.py, so adding them touches no shared file.
Only the hub's own controls run them: the model has no reason to call them, and a demo row
has nothing real to lose.

The Updates rework adds a fake UPDATE source here too, so the Updates panel (and its Apply / Preview / auto-update
switches) can be tried with no real program: provider `demo_updates` (`updates_only`, group other) with `check_demo_updates`
(lists three fake findings) and `apply_demo_update` (confirm-gated; applies one: `demo-alpha` is done, `demo-beta` is
started, `demo-fail` always fails, so every state can be seen).  It lives only in this file and only works while the
demo provider is on (hub_demo_provider start); with the demo off the check lists nothing and the apply refuses.  It
changes nothing outside one small file in the hub's own folder (demo_updates.json), and runs no program.
"""

import json
import os

# Its own group, not `downloads`: the model's download tools (dl_add and friends) should not grow
# three demo-only tools, and the hub's own controls call these by name, not through routing.
TOOL_GROUP = "hub_demo"
TEST_CHECKLIST_GROUP = {
    "label": "Downloads (hub demo controls)",
    "blurb": "Pause / resume / cancel for the Download Hub's fake demo downloads (the buttons on the demo rows run these).",
}

_ID = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "the demo download's own id, e.g. climb (without the demo: prefix)"}},
    "required": ["id"],
}


def _run(verb, args):
    try:
        from ..hub import demo
        from ..hub.store import Store
        return demo.control(Store(), verb, (args or {}).get("id"))
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return {"ok": False, "error": "hub_demo_%s failed: %s" % (verb, exc)}


def tool_hub_demo_pause(args):
    return _run("pause", args)


def tool_hub_demo_resume(args):
    return _run("resume", args)


def tool_hub_demo_cancel(args):
    return _run("cancel", args)


# ---------------------------------------------------------------------------
# the fake update source (the Updates rework)
# ---------------------------------------------------------------------------

_FAKE_UPDATES = (
    {"id": "demo-alpha", "title": "Demo Alpha (fake)", "installed": "1.0", "available": "1.1", "source": "demo"},
    {"id": "demo-beta", "title": "Demo Beta (fake)", "installed": "2.3", "available": "2.4", "source": "demo"},
    {"id": "demo-fail", "title": "Demo Fail (fake)", "installed": "0.9", "available": "1.0", "source": "demo"},
)


def _is_chat_guest():
    """The same fail-closed rule as download_updates._is_chat_guest (kept here so this file needs no import)."""
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def _demo_on():
    from ..hub.store import Store
    return bool((Store().get_settings().get("demo") or {}).get("enabled"))


def _state_path():
    from ..hub import paths
    return paths.hub_dir() / "demo_updates.json"


def _applied():
    """The fake ids already applied.  A missing or damaged file means none."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return set(x for x in data.get("applied", []) if isinstance(x, str))
    except (OSError, ValueError, AttributeError):
        return set()


def _save_applied(done):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"applied": sorted(done)}), encoding="utf-8")
    os.replace(tmp, path)


def tool_check_demo_updates(args=None):
    """List the fake findings that are not applied yet.  Read-only.  Demo off: an empty, complete list."""
    try:
        if not _demo_on():
            return {"ok": True, "updates": [], "complete": True}
        done = _applied()
        return {"ok": True, "complete": True, "updates": [dict(u) for u in _FAKE_UPDATES if u["id"] not in done]}
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return {"ok": False, "error": "check_demo_updates failed: %s" % exc}


def tool_apply_demo_update(args=None):
    """Apply ONE fake update, or (dry_run) say what it would do.  Confirm-gated; refuses a chat guest and a stopped demo."""
    try:
        if _is_chat_guest():
            return {"ok": False, "error": "apply_demo_update is the owner's"}
        args = args if isinstance(args, dict) else {}
        local = args.get("id")
        known = {u["id"]: u for u in _FAKE_UPDATES}
        if not isinstance(local, str) or local not in known:
            return {"ok": False, "error": "there is no demo update called %s" % str(local)[:40]}
        if not _demo_on():
            return {"ok": False, "error": "the demo isn't running; start it first (hub_demo_provider start)"}
        title = known[local]["title"]
        if args.get("dry_run") is True:
            return {"ok": True, "state": "unchanged",
                    "plan": "Would %s %s from %s to %s (a pretend update: nothing real changes)." % (
                        "try to apply" if local == "demo-fail" else "apply", title,
                        known[local]["installed"], known[local]["available"]),
                    "message": "plan only"}
        if local == "demo-fail":
            return {"ok": False, "state": "failed", "message": "the demo update failed on purpose", "error": "demo failure"}
        done = _applied()
        done.add(local)
        _save_applied(done)
        if local == "demo-beta":
            return {"ok": True, "state": "started", "message": "started the fake installer; finish it in the demo program"}
        return {"ok": True, "state": "done", "message": "applied the fake update"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "apply_demo_update failed: %s" % exc}


TOOL_DOWNLOADS = [{
    "id": "demo_updates", "label": "Demo updates", "mode": "push", "kind": "other", "icon": "",
    "update_tool": "check_demo_updates", "update_interval_h": 0.5, "update_group": "other",
    "apply_tool": "apply_demo_update", "apply_timeout_s": 30, "updates_only": True,
}]

# The apply changes (a pretend) machine state, so it is confirm-gated like every apply tool.
TOOL_CONFIRM_REQUIRED = {"apply_demo_update"}

_APPLY_PARAMS = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "the fake update's id: demo-alpha, demo-beta or demo-fail"},
        "dry_run": {"type": "boolean", "description": "only say what would happen; change nothing"},
    },
    "required": ["id"],
}

TOOL_SCHEMAS = [
    {"name": "check_demo_updates", "description": "List the fake updates of the Download Hub demo provider (only while the demo is running; the Updates panel's demo source runs this).",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "apply_demo_update", "description": "Apply one fake update of the Download Hub demo provider, or with dry_run say what it would do (the Updates panel's Apply and Preview buttons run this for the demo source). Pretend only.",
     "parameters": _APPLY_PARAMS},
    {"name": "hub_demo_pause", "description": "Pause one fake demo download (Download Hub demo provider only; the Pause button on a demo row runs this).", "parameters": _ID},
    {"name": "hub_demo_resume", "description": "Resume one paused fake demo download (Download Hub demo provider only; the Resume button on a demo row runs this).", "parameters": _ID},
    {"name": "hub_demo_cancel", "description": "Cancel one fake demo download (Download Hub demo provider only; the Cancel button on a demo row runs this).", "parameters": _ID},
]

TOOLS = {
    "check_demo_updates": tool_check_demo_updates,
    "apply_demo_update": tool_apply_demo_update,
    "hub_demo_pause": tool_hub_demo_pause,
    "hub_demo_resume": tool_hub_demo_resume,
    "hub_demo_cancel": tool_hub_demo_cancel,
}

TOOL_KEYWORDS = {
    "check_demo_updates": {"demo updates": 10},
    "apply_demo_update": {"apply demo update": 10},
    "hub_demo_pause": {"pause demo download": 10, "pause the demo": 10},
    "hub_demo_resume": {"resume demo download": 10, "resume the demo": 10},
    "hub_demo_cancel": {"cancel demo download": 10, "cancel the demo": 10},
}

TEST_CHECKLIST = {
    "check_demo_updates": {
        "does": "Lists the three fake updates (demo-alpha, demo-beta, demo-fail) that are not applied yet; the Updates panel's demo source.",
        "steps": [
            {"run": {}, "expect": "With the demo running: ok: true, complete: true, three updates (fewer after some were applied). "
                                  "With the demo stopped: an empty, complete list."},
        ],
        "needs": ["The demo started with hub_demo_provider (action start)"],
        "watch": ["Fake data only; reads one small file (demo_updates.json) in the hub's folder and changes nothing."],
    },
    "apply_demo_update": {
        "does": "Applies one fake update (the Updates panel's Apply and Preview buttons run it for the demo source): "
                "demo-alpha is done, demo-beta is started, demo-fail always fails.",
        "steps": [
            {"run": {"id": "demo-alpha", "dry_run": True}, "expect": "Asks for confirmation, then ok: true with a plan sentence; nothing changes."},
            {"run": {"id": "demo-alpha"}, "expect": "Asks for confirmation, then ok: true, state done; demo-alpha leaves the list."},
            {"run": {"id": "demo-fail"}, "expect": "Asks for confirmation, then ok: false, state failed, 'the demo update failed on purpose'."},
            {"run": {"id": "nope"}, "expect": "ok: false, 'there is no demo update called nope'."},
        ],
        "needs": ["The demo started with hub_demo_provider (action start): with it stopped the answer is ok: false and says so"],
        "care": "Pretend only: it writes demo_updates.json in the hub's folder and runs no program.",
        "watch": ["Owner only: refuses a chat guest. Delete demo_updates.json in the hub folder to bring the three back."],
    },
    "hub_demo_pause": {
        "does": "Pauses one demo download; this is what the Pause button on a demo row runs.",
        "steps": [
            {"run": {"id": "climb"}, "expect": "ok: true, message 'paused' while the demo is running; the climb row shows Paused and its bar stops."},
            {"run": {"id": "<any demo row>"},
             "expect": "With the demo stopped or never started (hub_demo_provider action stop / clear): ok: false, 'the demo isn't running; start it first'; nothing changes."},
            {"run": {"id": "steam:12345"}, "expect": "ok: false, error saying it is not a demo download (nothing else is touched)."},
        ],
        "needs": ["The demo started with hub_demo_provider (action start)"],
        "watch": ["Running it a second time on the same row says 'already paused'.",
                  "With the demo stopped it says so in words ('the demo isn't running') and changes nothing."],
    },
    "hub_demo_resume": {
        "does": "Resumes one paused demo download; this is what the Resume button on a demo row runs.",
        "steps": [
            {"run": {"id": "climb"}, "expect": "On a paused row: ok: true, message 'resumed'; the bar carries on from where it stopped."},
            {"run": {"id": "stalled"}, "expect": "On a row that is not paused: ok: false, error 'not paused'."},
        ],
        "needs": ["The demo started with hub_demo_provider (action start) and one row paused"],
    },
    "hub_demo_cancel": {
        "does": "Cancels one demo download; this is what the Cancel button on a demo row runs.",
        "steps": [
            {"run": {"id": "indeterminate"}, "expect": "ok: true, message 'cancelled'; the row shows Cancelled and stays that way."},
            {"run": {"id": "nope"}, "expect": "ok: false, error 'there is no demo download called nope'."},
        ],
        "needs": ["The demo started with hub_demo_provider (action start)"],
        "watch": ["Running it a second time on the same row says 'already cancelled'.",
                  "Starting the demo again (hub_demo_provider start) brings every row back."],
    },
}
