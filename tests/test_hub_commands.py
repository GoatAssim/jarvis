"""Download Hub: the palette commands (/dl /dls /dlpause /dlpauseall /dlresume /dlresumeall /dlproviders).

download_hub.py ships them through TOOL_COMMANDS (the data lives in actions/download_hub_commands.py).
Checks that the real loader accepts all seven, that the data-only module is skipped quietly, what
each command turns into (the tool call), that a bare /dlpause can never pause everything, that no
name clashes with a built-in CLI name or a palette verb, and that every command has a Test
Checklist step.

Run: python tests/test_hub_commands.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-cmds-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import reserved_names, tool_commands, tool_loader  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}  {detail}")


logs = []
records = tool_loader.discover_actions(reserved_names=reserved_names.RESERVED_NAMES, logger=logs.append)
hub = [r for r in records if r.file == "download_hub.py"]
check("the loader finds the download hub tool file and it is valid", len(hub) == 1 and hub[0].valid, hub and hub[0].error)
rec = hub[0]
cmds = {c["name"]: c for c in rec.commands}

check("all seven commands load", sorted(cmds) == sorted(["dl", "dls", "dlpause", "dlpauseall", "dlresume", "dlresumeall", "dlproviders"]),
      sorted(cmds))
check("the data-only module is skipped (no record, nothing rejected)",
      not [r for r in records if r.file == "download_hub_commands.py"]
      and not [l for l in logs if "download_hub_commands" in l])
check("no command was dropped with a logged error", not [l for l in logs if "download_hub" in l and "command" in l.lower() and "drop" in l.lower()])

tools = set(rec.tools)
check("every command drives a tool of the same file", all(c["tool"] in tools for c in cmds.values()))
check("risk tiers: reads are safe, everything that changes something is caution",
      {n for n, c in cmds.items() if c["riskTier"] == "safe"} == {"dls", "dlproviders"}
      and {n for n, c in cmds.items() if c["riskTier"] == "caution"} == {"dl", "dlpause", "dlpauseall", "dlresume", "dlresumeall"})


def build(name, *words):
    return tool_commands.build_arguments(cmds[name], list(words))


args, err = build("dl", "https://example.com/a.zip")
check("/dl <link> -> dl_add {url}", args == {"url": "https://example.com/a.zip"} and err is None and cmds["dl"]["tool"] == "dl_add", (args, err))
args, err = build("dl")
check("/dl alone is refused before anything runs", args is None and "needs" in err, err)
args, err = build("dl", "a", "b")
check("/dl takes one link, not two words", args is None, err)
args, err = build("dls")
check("/dls -> dl_status {}", args == {} and cmds["dls"]["tool"] == "dl_status", (args, err))
args, err = build("dls", "abc123")
check("/dls <id> -> dl_status {id}", args == {"id": "abc123"}, (args, err))
args, err = build("dlpause", "abc123")
check("/dlpause <id> -> dl_pause {id}", args == {"id": "abc123"} and cmds["dlpause"]["tool"] == "dl_pause", (args, err))
args, err = build("dlpause")
check("a bare /dlpause is refused: it can never pause the whole queue by accident", args is None and "needs" in err, (args, err))
args, err = build("dlpauseall")
check("/dlpauseall -> dl_pause {all: true}", args == {"all": True} and cmds["dlpauseall"]["tool"] == "dl_pause", (args, err))
args, err = build("dlpauseall", "abc")
check("/dlpauseall takes no arguments", args is None, err)
args, err = build("dlresume", "abc123")
check("/dlresume <id> -> dl_resume {id}", args == {"id": "abc123"} and cmds["dlresume"]["tool"] == "dl_resume", (args, err))
args, err = build("dlresume")
check("a bare /dlresume is refused", args is None, err)
args, err = build("dlresumeall")
check("/dlresumeall -> dl_resume {all: true}", args == {"all": True}, (args, err))
args, err = build("dlproviders")
check("/dlproviders -> dl_providers {}", args == {} and cmds["dlproviders"]["tool"] == "dl_providers", (args, err))

# every argument name really is a parameter of the tool it feeds
props = {s["name"]: set(((s.get("parameters") or {}).get("properties") or {})) for s in rec.schemas}
bad = []
for n, c in cmds.items():
    for a in c.get("args") or []:
        if a["arg"] not in props[c["tool"]]:
            bad.append((n, a["arg"]))
    for k in (c.get("fixed") or {}):
        if k not in props[c["tool"]]:
            bad.append((n, k))
check("every typed or fixed argument is a real parameter of its tool", not bad, bad)

check("no name clashes with a reserved CLI name", not [n for n in cmds if n in reserved_names.RESERVED_NAMES])
data = (ROOT / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8")
verbs = set(re.findall(r'"verb":\s*"([^"]+)"', data))
for m in re.finditer(r'"aliases":\s*\[([^\]]*)\]', data):
    verbs |= set(re.findall(r'"([^"]+)"', m.group(1)))
check("no name clashes with a built-in palette verb or alias", not [n for n in cmds if n in verbs], sorted(set(cmds) & verbs))

# the Test Checklist walks every command
asks = " ".join(step.get("ask", "") for entry in (rec.checklist or {}).values() for step in entry.get("steps", []))
for n in cmds:
    check("the checklist has a step that types /%s" % n, re.search(r"(^|\s)/%s(\s|$)" % n, asks) is not None)
steps = (rec.checklist or {}).get("dl_provider_prefs", {}).get("steps", [])
check("the checklist covers the Steam update-size setting",
      any("notify_min_update_gb" in str(s.get("run")) for s in steps) and len(steps) >= 9)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
