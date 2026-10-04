"""Quick setups for the Channels panel (L.36-P4) -- pure data, no I/O.

A preset is nothing but the existing per-person switches written together:
dm / reply / tool, plus (when tool is on) which tools. Applying one goes
through user_admin.set_flag() and user_admin.set_tools() -- the very calls the
individual switches make -- so a preset can never produce a state the switches
cannot, and it can never touch what they do not touch: ownership, `send_dm`
and blocking are not part of any preset.

FIXED IN CODE, ON PURPOSE
-------------------------
The plan left "who defines the presets" open. They are fixed here rather than
read from an owner-editable file because a tool list is a permission, and a
file nobody has reviewed is the wrong place for one to be invented. Adding a
preset is a code change and shows up in review. Nothing here names a tool
that is not a real, deliberate grant:

    notify_owner   the one tool a guest is already steered to ("tell the
                   owner ...", L.28). It tells the owner something; it does
                   not act on the PC.

`risk` is for the panel: "narrow" removes access, "widen" adds some, "broad"
adds the most there is (every tool the platform allows) and is the only one
the panel asks you to confirm twice.
"""

# tools: None  -> the preset leaves tool use off (any stored list is untouched)
#        {"mode": "custom",  "allow": [...]}  -> only these tools
#        {"mode": "inherit", "allow": []}     -> every tool the platform allows
PRESETS = (
    {
        "id": "none", "label": "No access", "risk": "narrow",
        "summary": "Not answered, no DMs, no tools.",
        "dm": False, "reply": False, "tool": False, "tools": None,
    },
    {
        "id": "chat_only", "label": "Chat only", "risk": "widen",
        "summary": "Can DM and gets answers. No tools.",
        "dm": True, "reply": True, "tool": False, "tools": None,
    },
    {
        "id": "chat_notify", "label": "Chat + tell the owner", "risk": "widen",
        "summary": "Chat, and may ask Jarvis to pass a message to you "
                   "(notify_owner). Nothing else.",
        "dm": True, "reply": True, "tool": True,
        "tools": {"mode": "custom", "allow": ["notify_owner"]},
    },
    {
        "id": "trusted", "label": "Trusted", "risk": "broad",
        "summary": "Chat, plus every tool the platform allows. Owner-only "
                   "tools such as send_dm stay owner-only.",
        "dm": True, "reply": True, "tool": True,
        "tools": {"mode": "inherit", "allow": []},
    },
)

_BY_ID = {p["id"]: p for p in PRESETS}


def get(preset_id):
    """The preset with this id, or None."""
    return _BY_ID.get(str(preset_id or "").strip())


def ids():
    return [p["id"] for p in PRESETS]


def public_view():
    """What the panel and `channels-presets` show: a plain copy, so a caller
    cannot edit the module's own table."""
    out = []
    for p in PRESETS:
        tools = p["tools"]
        out.append({
            "id": p["id"], "label": p["label"], "summary": p["summary"],
            "risk": p["risk"],
            "dm": p["dm"], "reply": p["reply"], "tool": p["tool"],
            "tools": (None if tools is None else
                      {"mode": tools["mode"], "allow": list(tools["allow"])}),
        })
    return out
