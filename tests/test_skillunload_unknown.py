"""I-B3 / C10 -- `jarvis skillunload <name>` no longer says "unloaded" for a skill that
was never loaded in that scope.

Before: the CLI always printed {"unloaded": true} (exit 0), even for a typo or a skill
loaded somewhere else, so the web `/skillunload` toast claimed success for a no-op. Now it
prints {"error": ..., "unloaded": false} and exits 1; the web server already turns an
"error" reply into a 400 with that message.

Covers: `skill_stickiness.unload()` return value and `is_loaded()` (exact scope, no global
merge); the CLI end to end in a throwaway HOME -- loaded name still unloads (exit 0), unknown
name errors (exit 1, stored data untouched), unloading twice, a conversation-only unload of a
globally loaded skill (error with a hint, global entry kept), `--all` unchanged, usage error.

No network, no live model.   Run: python3 tests/test_skillunload_unknown.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "jarvis-cli"
sys.path.insert(0, str(PKG))

HOME = tempfile.mkdtemp(prefix="jarvis_skillunload_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")
os.environ["PYTHONPATH"] = str(PKG) + os.pathsep + os.environ.get("PYTHONPATH", "")

from jarvis import skill_stickiness as ss  # noqa: E402  (after HOME is redirected)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def cli(*args):
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], env=dict(os.environ),
                          cwd=str(PKG), capture_output=True, text=True, timeout=90)
    try:
        data = json.loads(proc.stdout.strip())
    except ValueError:
        data = None
    return proc.returncode, data, proc


def reset():
    ss.unload_all(None)
    ss.unload_all("conv-1")


def test_unload_reports_whether_it_removed_anything():
    reset()
    ss.load("conv-1", "Alpha")
    check("is_loaded sees it in its own scope", ss.is_loaded("conv-1", "Alpha"))
    check("is_loaded is exact: not in another scope", not ss.is_loaded("conv-2", "Alpha"))
    check("is_loaded does not merge the global scope", not ss.is_loaded(None, "Alpha"))
    check("unload of a loaded name returns True", ss.unload("conv-1", "Alpha") is True)
    check("unload of it again returns False", ss.unload("conv-1", "Alpha") is False)
    check("unload of a blank name returns False", ss.unload("conv-1", "  ") is False)
    check("nothing left", ss.get_loaded("conv-1") == [])


def test_cli_unloads_a_loaded_skill():
    reset()
    ss.load("conv-1", "Alpha")
    code, data, proc = cli("skillunload", "Alpha", "conv-1")
    check("loaded skill: exit 0", code == 0, proc.stderr)
    check("loaded skill: unloaded true, scope named",
          bool(data) and data.get("unloaded") is True and data.get("scope") == "conv-1", data)
    check("loaded skill: actually gone", ss.get_loaded("conv-1") == [])


def test_cli_refuses_a_name_that_was_never_loaded():
    reset()
    ss.load("conv-1", "Alpha")
    code, data, proc = cli("skillunload", "Ghost", "conv-1")
    check("unknown name: exit 1", code == 1, (code, proc.stderr))
    check("unknown name: error says it isn't loaded",
          bool(data) and "isn't loaded" in data.get("error", ""), data)
    check("unknown name: unloaded is false, not true",
          bool(data) and data.get("unloaded") is False, data)
    check("unknown name: other loaded skills untouched", ss.get_loaded("conv-1") == ["Alpha"])


def test_unloading_twice_is_an_error_the_second_time():
    reset()
    ss.load(None, "Beta")
    first = cli("skillunload", "Beta")
    second = cli("skillunload", "Beta")
    check("first global unload succeeds", first[0] == 0 and first[1].get("unloaded") is True, first[1])
    check("second says nothing to unload", second[0] == 1 and "nothing to unload" in second[1].get("error", ""),
          second[1])


def test_conversation_unload_of_a_global_skill_explains_itself():
    reset()
    ss.load(None, "Gamma")
    code, data, _ = cli("skillunload", "Gamma", "conv-1")
    check("global skill, conversation unload: exit 1", code == 1, data)
    check("the error says it is loaded for every conversation",
          bool(data) and "every conversation" in data.get("error", "") and "without a conversation id" in data["error"],
          data)
    check("the global entry is still there", ss.get_loaded("conv-9") == ["Gamma"])


def test_all_and_usage_are_unchanged():
    reset()
    ss.load("conv-1", "Alpha")
    code, data, _ = cli("skillunload", "--all", "conv-1")
    check("--all still succeeds", code == 0 and data.get("unloaded_all") is True, data)
    check("--all cleared the scope", ss.get_loaded("conv-1") == [])
    code, data, _ = cli("skillunload")
    check("no name: the usage error", code == 1 and "usage" in (data or {}).get("error", ""), data)


if __name__ == "__main__":
    for name in [n for n in sorted(globals()) if n.startswith("test_")]:
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAIL.append((name, repr(exc)))
            print(f"FAILED  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
