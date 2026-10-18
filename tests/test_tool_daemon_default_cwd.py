"""A daemon shipped inside the jarvis package must not run with its cwd in the install.

On Windows a process keeps its working folder open, so a daemon started in
`...\\site-packages\\jarvis\\actions` (the Download hub: restart "always", autostart) made
`pip install . --force-reinstall` fail and came back whenever it was killed.
`tool_daemons._default_cwd` gives such daemons the config folder instead; a tool folder
outside the package keeps the old default (its own folder), and an explicit `cwd` still wins.

Run: python3 tests/test_tool_daemon_default_cwd.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-dcwd-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import daemons, tool_daemons  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name} {detail}")


def entry(base_dir, **over):
    item = {"id": "worker", "command": ["{python}", "{tool_dir}/worker.py"]}
    item.update(over)
    entries, errors = tool_daemons.validate_daemons([item], "tool.py", base_dir)
    return (entries[0] if entries else None), errors


try:
    pkg_actions = ROOT / "jarvis-cli" / "jarvis" / "actions"
    e, errs = entry(pkg_actions)
    check("a shipped daemon is valid", e is not None, errs)
    check("its cwd is the config folder, not the package", e and e["cwd"] == str(daemons.JARVIS_DIR), e and e["cwd"])
    check("and not inside the package", e and not Path(e["cwd"]).resolve().is_relative_to(ROOT / "jarvis-cli" / "jarvis"))
    check("its command still points at the tool's folder", e and e["argv"][1] == f"{pkg_actions}/worker.py", e and e["argv"])

    outside = Path(tempfile.mkdtemp(prefix="jarvis-dcwd-tool-"))
    e, _ = entry(outside)
    check("a tool outside the package keeps its own folder as cwd", e and e["cwd"] == str(outside), e and e["cwd"])

    e, _ = entry(pkg_actions, cwd="{tool_dir}/data")
    check("an explicit cwd still wins", e and e["cwd"] == f"{pkg_actions}/data", e and e["cwd"])

    hub = (ROOT / "jarvis-cli" / "jarvis" / "actions" / "download_hub.py")
    src = hub.read_text(encoding="utf-8")
    check("the Download hub declares no cwd of its own (so it gets the safe default)",
          '"cwd"' not in src.split("DAEMONS = [", 1)[1].split("]\n", 1)[0])
finally:
    shutil.rmtree(HOME, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
