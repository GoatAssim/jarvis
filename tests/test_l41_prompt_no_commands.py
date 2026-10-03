"""L.41 -- the system prompt carries no saved-commands text.

Standalone, plain-assert, like the rest of tests/. Run directly:
    python3 tests/test_l41_prompt_no_commands.py

What L.41 removed (owner's call, recorded in the master plan):
  * the 'Commands:' / 'Saved commands:' listing (ai_client._commands_context)
  * the frequently-used-commands block (stats.frequent_commands_context)
  * the COMMANDS clause, in all three verbosities (_TOOL_WORKFLOW_CLAUSES)

What it deliberately kept, and what this file pins so a later tidy-up does not
take it by accident:
  * search_commands stays offered to the model and stays in PROTECTED_TOOLS
  * the discovery nudge (search_tools sentence + search_commands sentence)
  * the tools blurb, with its confirm-before-destructive rule
  * every OTHER workflow clause (RADIOS, ...)

The behavioural half seeds real saved commands and real usage counts on disk
first. Without that, "the prompt names no command" would pass vacuously on a
machine with an empty commands.json.
"""

import inspect
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import ai_client, commands_config, stats, tool_disable  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402
from jarvis.tool_router import RouteResult  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# Names chosen so they cannot occur in the prompt by accident.
SEEDED = {
    "zzl41-spotify": {"description": "Update the zzl41 playlist", "run": "echo a"},
    "zzl41-backup": {"description": "Back up the zzl41 folder", "run": "echo b"},
    "zzl41-deploy": {"description": "Ship the zzl41 build", "run": "echo c"},
}
commands_config.save_commands_dict(SEEDED)
for _ in range(3):
    stats.bump("zzl41-spotify")
stats.bump("zzl41-backup")

# Sanity: the seed is real, so absence below means something.
check("seed: commands are on disk", set(commands_config.load_commands_dict()) >= set(SEEDED))
check("seed: usage counts are on disk", dict(stats.top(5)).get("zzl41-spotify") == 3, stats.top(5))

# Phrases that only the removed text contained.
FORBIDDEN = (
    "Commands:", "Saved commands", "most frequently used", "COMMANDS:",
    "only some are listed above", "is only a partial preview", "more not shown here",
    "Unsure of a saved command",
)
SEEDED_NAMES = tuple(SEEDED)


def system_text(mode, route, tools_enabled=True):
    profile = ai_client._MODE_BY_NAME[mode]
    msgs = ai_client._build_messages({}, "run my spotify command", tools_enabled, profile, None, route=route)
    return "\n\n".join(m["content"] for m in msgs if m["role"] == "system")


# --- 1. the built prompt, every capacity mode x every router outcome -------------------
ROUTES = {
    "no route (tools off)": (None, False),
    "router has no opinion": (RouteResult([], []), True),
    "commands group matched": (RouteResult(["search_commands", "run_command", "run_chain"], ["commands"]), True),
    "a different group matched": (RouteResult(["get_time"], ["system"]), True),
}
for mode in ai_client.PROMPT_MODES:
    for label, (route, tools_on) in ROUTES.items():
        text = system_text(mode, route, tools_on)
        hits = [p for p in FORBIDDEN + SEEDED_NAMES if p in text]
        check(f"[{mode} / {label}] no commands listing, frequent-commands block, clause or command name",
              not hits, hits)

# --- 2. the pieces are gone from the code, not just empty ------------------------------
check("_commands_context no longer exists", not hasattr(ai_client, "_commands_context"))
check("stats.frequent_commands_context no longer exists", not hasattr(stats, "frequent_commands_context"))
params = set(inspect.signature(ai_client._system_prompt_parts).parameters)
check("_system_prompt_parts takes no commands_ctx / freq_ctx", not ({"commands_ctx", "freq_ctx"} & params), params)
check("_build_messages takes no commands dict",
      "commands" not in inspect.signature(ai_client._build_messages).parameters)
check("no workflow clause is keyed on the commands tools",
      not any({"search_commands", "run_command", "run_chain"} & set(entry[0])
              for entry in ai_client._TOOL_WORKFLOW_CLAUSES))
dead = {"max_commands", "desc_max_len", "include_freq"}
check("no capacity mode still carries the dead listing knobs",
      not any(dead & set(m) for m in ai_client.PROMPT_MODE_DEFS),
      [m["name"] for m in ai_client.PROMPT_MODE_DEFS if dead & set(m)])
check("the mode summaries no longer promise trimmed commands",
      not any("commands" in m.get("summary", "").lower() for m in ai_client.PROMPT_MODE_DEFS))

# An old hand-edited ai_config.json may still carry the retired knob; it must be ignored, not crash.
profile = ai_client._prompt_profile("groq", {"prompt_mode": "compact", "compact_max_commands": 99})
check("a leftover compact_max_commands in config is ignored", "max_commands" not in profile)

# --- 3. the workflow-notes function emits nothing about commands -----------------------
OFFERED = {"search_commands", "run_command", "run_chain"}
for compact, ultra, label in ((False, False, "full"), (True, False, "compact"), (True, True, "ultra")):
    note = ai_client._tool_workflow_notes(OFFERED, compact, ultra)
    check(f"[{label}] offering the commands tools adds no workflow note", note == "", repr(note))
    legacy = ai_client._tool_workflow_notes(None, compact, ultra)
    check(f"[{label}] the unconditional (legacy) path names no saved command either",
          "search_commands" not in legacy and "COMMANDS" not in legacy, legacy[:120])
check("the OTHER clauses survive (RADIOS still emitted in full mode)",
      "RADIOS" in ai_client._tool_workflow_notes({"wifi_set"}, False, False))

# --- 4. what was kept ------------------------------------------------------------------
check("search_commands is still protected", "search_commands" in tool_disable.PROTECTED_TOOLS)
check("search_commands is still offered with the discovery set",
      any(s.get("name") == "search_commands" for s in system_tools.DISCOVERY_AND_COMMANDS_SCHEMAS))
nudge = system_text("compact", RouteResult([], []))
check("the discovery nudge is intact: its search_tools sentence...",
      "call search_tools with a keyword" in nudge, nudge[-400:])
check("...and its search_commands sentence (the owner kept it)",
      "try search_commands instead" in nudge, nudge[-400:])
check("the compact tools blurb keeps the confirm-before-destructive rule",
      "Confirm before install/delete/off/eval." in ai_client._tools_blurb(True, False))
check("the ultra tools blurb keeps it too",
      "Confirm before install/delete/off/eval." in ai_client._tools_blurb(True, True))
check("the full tools blurb keeps it too",
      "Confirm before launch/delete/install/eval." in ai_client._tools_blurb(False, False))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
