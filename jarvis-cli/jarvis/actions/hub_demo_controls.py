"""The Download Hub demo provider's pause / resume / cancel tools (brief 13, part A).

Three small tools the demo provider names in its `controls` (hub/demo.py `CONTROLS`), so the
Pause / Resume / Cancel buttons on the demo rows have something to run.  Each takes the demo
row's own id (`climb`, not `demo:climb`) and calls `hub.demo.control`; every rule lives there.

They live in their own file, not in download_hub.py, so adding them touches no shared file.
Only the hub's own controls run them: the model has no reason to call them, and a demo row
has nothing real to lose.
"""

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


TOOL_SCHEMAS = [
    {"name": "hub_demo_pause", "description": "Pause one fake demo download (Download Hub demo provider only; the Pause button on a demo row runs this).", "parameters": _ID},
    {"name": "hub_demo_resume", "description": "Resume one paused fake demo download (Download Hub demo provider only; the Resume button on a demo row runs this).", "parameters": _ID},
    {"name": "hub_demo_cancel", "description": "Cancel one fake demo download (Download Hub demo provider only; the Cancel button on a demo row runs this).", "parameters": _ID},
]

TOOLS = {
    "hub_demo_pause": tool_hub_demo_pause,
    "hub_demo_resume": tool_hub_demo_resume,
    "hub_demo_cancel": tool_hub_demo_cancel,
}

TOOL_KEYWORDS = {
    "hub_demo_pause": {"pause demo download": 10, "pause the demo": 10},
    "hub_demo_resume": {"resume demo download": 10, "resume the demo": 10},
    "hub_demo_cancel": {"cancel demo download": 10, "cancel the demo": 10},
}

TEST_CHECKLIST = {
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
