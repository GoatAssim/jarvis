"""L.30 ratchet: nothing outside the notification module keeps its own list of levels, notification
types, positions or looks.

The rule (notify_registry.py, "Nothing else keeps its own list"): a new action, level, kind, position,
look or tone is ONE row in notify_registry; no page, tool or panel types out a level name, a channel id
or a position. They read the table through the catalog. This test keeps that true.

It reads the ids and labels FROM the registry (so it is not itself a list), then fails when a file under
web/public/, a tool's *_ui/ folder or web/server.js:

  1. contains a level label (a preset's name, e.g. Silent) as a quoted string next to another level label
     or next to a digit ("1 . Silent", [1, "Silent"], "Silent", "Standard"),
  2. contains three or more registry action ids as quoted strings within about 400 characters, or
  3. contains [1, 5], <= 5 or 1-5 on a line that talks about a level.

A Python half scans the notification-related .py files for range(1, 6), `1 <= level <= 5` and "1-5" on a
line that talks about a level. The registry itself and model-facing text are allowed.

A single line can opt out with `registry-ok: <reason>` (in a // or # comment). Opt-outs are counted against
a baseline, like the E5 ratchet in test_one_notification_system.py: adding one means changing the number
below in the same patch and saying why.

Run: python3 tests/test_no_hardcoded_notify_lists.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import notify_registry as reg  # noqa: E402

PASS, FAIL = [], []

# How many lines may carry `registry-ok:` across the whole scan. Every one needs a reason on the line.
OPT_OUT_BASELINE = 0

WEB = ROOT / "web"
JS_FILES = (
    sorted((WEB / "public").rglob("*.js"))
    + sorted((ROOT / "jarvis-cli" / "jarvis" / "actions").glob("*_ui/*.js"))
    + [WEB / "server.js"]
)
# The notification-related Python. The registry itself is the one place the table may live.
PY_FILES = [
    ROOT / "jarvis-cli" / "jarvis" / name
    for name in ("notifier.py", "scheduler.py", "digest.py", "cli.py", "doctor.py", "tools.py")
] + sorted((ROOT / "jarvis-cli" / "jarvis" / "hub").glob("*.py")) + [
    ROOT / "jarvis-cli" / "jarvis" / "actions" / name
    for name in ("scheduler_tools.py", "download_hub.py", "notification_test.py")
]

OPT_OUT = re.compile(r"registry-ok:\s*\S")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# ---------------------------------------------------------------------------------------------------------
# the detectors (also unit-tested below, so the scan cannot pass by finding nothing)
# ---------------------------------------------------------------------------------------------------------

def _level_names():
    """The labels a screen would show (Silent, Standard, ...), exactly as the registry spells them. The lower
    case ids (\"confirm\", \"persistent\") are also ordinary words in UI code, so they are not matched."""
    return sorted({str(p["name"]) for p in reg.PRESETS}, key=str.lower)


def _name_alt():
    return "|".join(re.escape(n) for n in _level_names())


def level_label_hits(text):
    """(line_number, snippet) for each line that spells out level labels. See rule 1."""
    alt = _name_alt()
    quoted_label = re.compile(r"""["'`]\s*(?:\d+\s*\S{0,2}\s*)?(?:%s)\s*["'`]""" % alt)
    digit_label = re.compile(r"""["'`]\s*\d+\s*\S{0,2}\s*(?:%s)\s*["'`]""" % alt)
    pair = re.compile(r"""\d["']?\s*,\s*["'`](?:%s)["'`]""" % alt)
    hits = []
    lines = text.split("\n")
    for i, line in enumerate(lines, 1):
        if OPT_OUT.search(line):
            continue
        quoted = quoted_label.findall(line)
        window = " ".join(lines[max(0, i - 2):i + 1])
        distinct = {m.group(0).strip("\"'` ") for m in quoted_label.finditer(window)}
        if digit_label.search(line) or pair.search(line) or (quoted and len(distinct) >= 2):
            hits.append((i, line.strip()[:140]))
    return hits


def action_id_hits(text):
    """Line numbers where three or more distinct registry action ids appear as quoted strings within ~400
    characters. See rule 2."""
    ids = [a["id"] for a in reg.ACTIONS]
    pat = re.compile(r"""["'`](%s)["'`]""" % "|".join(re.escape(i) for i in ids))
    hits, seen_line = [], set()
    for m in pat.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        if line_no in seen_line:
            continue
        window = text[m.start():m.start() + 400]
        if len({x for x in pat.findall(window)}) >= 3:
            line_text = text.split("\n")[line_no - 1]
            if OPT_OUT.search(line_text):
                continue
            seen_line.add(line_no)
            hits.append((line_no, line_text.strip()[:140]))
    return hits


_RANGE_PAT = re.compile(r"\[\s*1\s*,\s*5\s*\]|<=\s*5\b|\b1\s*-\s*5\b|\b1\s*(?:to|\.\.|\u2013)\s*5\b")


def level_range_hits(text):
    """Lines that write the old 1-5 range next to the word level. See rule 3 (and the Python half)."""
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        if OPT_OUT.search(line):
            continue
        if _RANGE_PAT.search(line) and re.search(r"level", line, re.I):
            hits.append((i, line.strip()[:140]))
    return hits


def python_hits(text):
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        if OPT_OUT.search(line):
            continue
        if re.search(r"range\(\s*1\s*,\s*6\s*\)", line) or re.search(r"1\s*<=\s*\w*level\w*\s*<=\s*5", line):
            hits.append((i, line.strip()[:140]))
        elif re.search(r"\b1\s*-\s*5\b", line) and re.search(r"level", line, re.I):
            hits.append((i, line.strip()[:140]))
    return hits


def opt_outs(text):
    return sum(1 for line in text.split("\n") if OPT_OUT.search(line))


# ---------------------------------------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------------------------------------

def test_the_detectors_work():
    first = reg.PRESETS[0]
    name = str(first["name"])
    ids = [a["id"] for a in reg.ACTIONS][:3]
    check("rule 1 catches a digit-prefixed label", bool(level_label_hits('const x = "1 \u00b7 %s";' % name)))
    check("rule 1 catches a [1, \"Label\"] pair", bool(level_label_hits('const x = [[1, "%s"]];' % name)))
    second = str(reg.PRESETS[1]["name"])
    check("rule 1 catches two labels side by side", bool(level_label_hits('const x = ["%s", "%s"];' % (name, second))))
    check("rule 1 lets one bare mention in prose through", not level_label_hits('toast("%s was applied");' % name))
    check("rule 1 honours registry-ok", not level_label_hits('const x = "1 \u00b7 %s"; // registry-ok: demo' % name))
    check("rule 2 catches three ids close together", bool(action_id_hits("x = ['%s', '%s', '%s'];" % tuple(ids))))
    check("rule 2 lets two ids through", not action_id_hits("x = ['%s', '%s'];" % tuple(ids[:2])))
    check("rule 2 lets three ids far apart through", not action_id_hits("a('%s');\n%s\nb('%s');\n%s\nc('%s');" % (ids[0], "x" * 500, ids[1], "y" * 500, ids[2])))
    check("rule 3 catches `[1, 5]` and `<= 5` and `1-5` on a level line",
          bool(level_range_hits("const levelRange = [1, 5];")) and bool(level_range_hits("if (level <= 5) {}"))
          and bool(level_range_hits("// level 1-5")))
    check("rule 3 lets a range with no level in it through", not level_range_hits("for (i = 1; i <= 5; i++) {}"))
    check("the Python half catches range(1, 6) and 1 <= level <= 5 and a 1-5 level comment",
          bool(python_hits("for n in range(1, 6): pass")) and bool(python_hits("if 1 <= level <= 5:"))
          and bool(python_hits("# the level is 1-5")))
    check("the Python half lets a count of 1-5 copies through", not python_hits("# copies: 1-5"))


def test_there_are_files_to_scan():
    check("the web page's scripts are scanned", any(p.name == "notifications.js" for p in JS_FILES), [p.name for p in JS_FILES][:5])
    check("server.js is scanned", WEB / "server.js" in JS_FILES and (WEB / "server.js").exists())
    check("tool panels are scanned", any("download_hub" not in str(p) and p.parent.name.endswith("_ui") for p in JS_FILES) or any(p.parent.name == "hub_ui" for p in JS_FILES))
    check("the new test tool's panel is scanned", any(p.parent.name == "notification_test_ui" for p in JS_FILES))
    check("the notification Python is scanned", all(p.exists() for p in PY_FILES), [str(p) for p in PY_FILES if not p.exists()])


def test_no_page_keeps_its_own_notification_lists():
    total_opt_outs = 0
    for path in JS_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        total_opt_outs += opt_outs(text)
        for rule, hits in (("a level label spelled out", level_label_hits(text)),
                           ("three or more notification types listed", action_id_hits(text)),
                           ("the old 1-5 level range", level_range_hits(text))):
            check(f"{rel}: no {rule}", not hits, hits[:3])
    check("registry-ok opt-outs are inside the baseline (%d)" % OPT_OUT_BASELINE, total_opt_outs <= OPT_OUT_BASELINE,
          "%d opt-outs; change OPT_OUT_BASELINE in the same patch and say why" % total_opt_outs)


def test_no_python_outside_the_registry_hardcodes_the_level_range():
    total = 0
    for path in PY_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        total += opt_outs(text)
        hits = python_hits(text)
        check(f"{path.relative_to(ROOT)}: no range(1, 6), `1 <= level <= 5` or \"1-5\" level wording", not hits, hits[:3])
    check("registry-ok opt-outs in Python are inside the baseline", total <= OPT_OUT_BASELINE, total)


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
