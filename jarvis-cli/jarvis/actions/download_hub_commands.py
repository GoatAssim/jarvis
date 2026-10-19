"""Palette commands for the Download Hub tools (data only, no tools of its own).

`download_hub.py` hands this list to the loader as the tool file's palette commands, so they show up
in the Ask box's "/" palette as typed twins of the tools.  The loader silently skips a file in
actions/ that defines no tools, so this module is safe to live next to the tool file.

Each entry is validated by tool_commands.validate_commands against download_hub's real tool
schemas: an unknown tool, a missing argument or a name that clashes with a built-in is logged and
that one command dropped, never the whole file.

"Pause everything" and "resume everything" are their own commands (`fixed` arguments) rather than
an `[id|all]` argument, so a bare /dlpause can't pause the whole queue by accident.
"""

HUB_COMMANDS = [
    {"name": "dl", "tool": "dl_add", "riskTier": "caution",
     "summary": "download a link into the Download Hub",
     "args": [{"name": "url", "arg": "url", "required": True, "hint": "a link to download"}]},
    {"name": "dls", "tool": "dl_status", "riskTier": "safe",
     "summary": "what is downloading right now",
     "args": [{"name": "id", "arg": "id", "required": False, "hint": "a download id (optional)"}]},
    {"name": "dlpause", "tool": "dl_pause", "riskTier": "caution",
     "summary": "pause one download",
     "args": [{"name": "id", "arg": "id", "required": True, "hint": "a download id"}]},
    {"name": "dlpauseall", "tool": "dl_pause", "riskTier": "caution",
     "summary": "pause every running download", "fixed": {"all": True}},
    {"name": "dlresume", "tool": "dl_resume", "riskTier": "caution",
     "summary": "resume one paused download",
     "args": [{"name": "id", "arg": "id", "required": True, "hint": "a download id"}]},
    {"name": "dlresumeall", "tool": "dl_resume", "riskTier": "caution",
     "summary": "resume every paused download", "fixed": {"all": True}},
    {"name": "dlproviders", "tool": "dl_providers", "riskTier": "safe",
     "summary": "which sources feed the Download Hub (Steam, Playnite...)"},
]
