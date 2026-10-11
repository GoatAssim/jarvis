"""L.24 T5 -- trim the static text that rides on every call.

Standalone, plain-assert, like the rest of tests/. Run directly:
    python3 tests/test_l24_t5_static_text.py

What T5 changed
  * skills.catalog_text(compact=, ultra=): a shorter header and a harder description
    clip in the cheaper capacity modes. FULL mode is byte-identical to before.
  * ai_client._tools_blurb(compact=True, ultra=False): reworded shorter. The
    full and ultra blurbs are untouched.
  * The desktop workflow note was ALREADY emitted only when the desktop group is
    active (route.groups); this file pins that so it stays true.

What it keeps pinned (other tests rely on it): "invent", "search_tools" and the
confirm-before-destructive sentence in every blurb.

Nothing here measures real tokens; it compares characters, which is what the
prompt-size claim in the plan is built on. A real number needs measure_l24.py.
"""

import os
import shutil
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

from jarvis import ai_client, skills  # noqa: E402
from jarvis.tool_router import RouteResult  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# The text as it was before T5, copied here so "shorter than before" is a real comparison.
OLD_FULL_HEADER = (
    "Skills available (instructions you can load on demand). Call "
    "load_skill with the name when a task matches one \u2014 the description "
    "is all you have until you do:"
)
OLD_COMPACT_BLURB = (
    "Tools are listed by name only. Call one when you need it. "
    "If it needs arguments you don't know, call it with no arguments \u2014 "
    "you will get its schema, then call it again. "
    "Never invent a tool name \u2014 if you don't see one for what you need, "
    "call search_tools first. "
    "Confirm before install/delete/off/eval."
)

tmp = Path(tempfile.mkdtemp(prefix="jarvis_skills_t5_"))
skills.SKILLS_DIR = tmp / "skills"
LONG = ("Zebra " * 60).strip()           # far longer than every clip
skills.create_skill("Zzt5 Report", LONG, "## Steps\n1. go\n", ["zzt5"])

# --- 1. the skills catalog ----------------------------------------------------------
full = skills.catalog_text()
compact = skills.catalog_text(compact=True)
ultra = skills.catalog_text(compact=True, ultra=True)


def line_of(text):
    return next((l for l in text.splitlines() if "Zzt5 Report" in l), "")


check("full mode keeps the old header byte for byte", full.splitlines()[0] == OLD_FULL_HEADER, full.splitlines()[0])
check("full mode still clips at DESC_MAX", len(line_of(full).split(": ", 1)[1]) == skills.DESC_MAX, len(line_of(full)))
check("compact header is shorter than the old one", len(compact.splitlines()[0]) < len(OLD_FULL_HEADER))
check("ultra header is shorter than compact's", len(ultra.splitlines()[0]) < len(compact.splitlines()[0]))
check("compact clips descriptions at DESC_MAX_COMPACT", len(line_of(compact).split(": ", 1)[1]) == skills.DESC_MAX_COMPACT)
check("ultra clips descriptions at DESC_MAX_ULTRA", len(line_of(ultra).split(": ", 1)[1]) == skills.DESC_MAX_ULTRA)
check("the clip is marked with an ellipsis", line_of(compact).endswith("\u2026") and line_of(ultra).endswith("\u2026"))
check("each step down is smaller", len(ultra) < len(compact) < len(full), (len(ultra), len(compact), len(full)))
check("every mode still tells the model how to load a skill", all("load_skill" in t for t in (full, compact, ultra)))
check("every mode still names the skill", all("Zzt5 Report" in t for t in (full, compact, ultra)))

# A short description is never padded or altered.
skills.create_skill("Zzt5 Short", "Does a small thing.", "body", [])
check("a short description is untouched in every mode",
      all("- Zzt5 Short: Does a small thing." in t for t in (skills.catalog_text(), skills.catalog_text(compact=True),
                                                           skills.catalog_text(compact=True, ultra=True))))

# The "(+N more)" tail still works with the new signature.
over = skills.catalog_text(max_skills=1, compact=True)
check("max_skills still truncates and says how many are hidden", "more" in over and "list_skills" in over, over[-120:])

# An empty store still costs nothing, in every mode.
skills.SKILLS_DIR = tmp / "empty-skills"
check("no skills = empty block in every mode",
      skills.catalog_text() == "" and skills.catalog_text(compact=True) == "" and skills.catalog_text(compact=True, ultra=True) == "")
skills.SKILLS_DIR = tmp / "skills"

# --- 2. the tools blurb --------------------------------------------------------------
new_compact = ai_client._tools_blurb(True, False)
new_ultra = ai_client._tools_blurb(True, True)
new_full = ai_client._tools_blurb(False, False)
check("the compact blurb is shorter than before", len(new_compact) < len(OLD_COMPACT_BLURB), (len(new_compact), len(OLD_COMPACT_BLURB)))
check("compact keeps 'invent', search_tools and the confirm rule",
      "invent" in new_compact.lower() and "search_tools" in new_compact and "Confirm before install/delete/off/eval." in new_compact, new_compact)
check("compact still explains the no-arguments schema trick", "no" in new_compact.lower() and "schema" in new_compact, new_compact)
check("the ultra blurb is unchanged", new_ultra.startswith("Tools listed by name only") and "Confirm before install/delete/off/eval." in new_ultra, new_ultra)
check("the full blurb is unchanged", new_full == "ONLY call tools that appear in your tool list. Never invent a tool name. Confirm before launch/delete/install/eval.", new_full)

# --- 3. the built prompt, per capacity mode -------------------------------------------
DESKTOP_NOTE = "Desktop automation acts on the real mouse"


def system_text(mode, route):
    profile = ai_client._MODE_BY_NAME[mode]
    msgs = ai_client._build_messages({}, "hello there", True, profile, None, route=route)
    return "\n\n".join(m["content"] for m in msgs if m["role"] == "system")


for mode in ai_client.PROMPT_MODES:
    profile = ai_client._MODE_BY_NAME[mode]
    ultra_mode = profile.get("tool_schema_style") == "name_only"
    compact_mode = profile.get("compact_tools_blurb", False)
    text = system_text(mode, RouteResult(["get_time"], ["system"]))
    if ultra_mode:
        want = skills.CATALOG_HEADER_ULTRA
    elif compact_mode:
        want = skills.CATALOG_HEADER_COMPACT
    else:
        want = skills.CATALOG_HEADER_FULL
    check(f"[{mode}] the prompt carries that mode's skills header", want in text, text[:200])
    others = {skills.CATALOG_HEADER_ULTRA, skills.CATALOG_HEADER_COMPACT, skills.CATALOG_HEADER_FULL} - {want}
    check(f"[{mode}] ...and none of the other modes' headers", not any(o in text for o in others))
    check(f"[{mode}] no desktop workflow note when the desktop group is not active", DESKTOP_NOTE not in text)
    on = system_text(mode, RouteResult(["click"], ["desktop"]))
    check(f"[{mode}] the desktop workflow note IS there when the desktop group is active", DESKTOP_NOTE in on)

# The compact and ultra catalogs are smaller than the full one for the same skills (the
# T5 saving itself; section 1 already compares each step).
for mode in ("compact", "ultra"):
    profile = ai_client._MODE_BY_NAME[mode]
    new_catalog = skills.catalog_text(compact=True, ultra=profile.get("tool_schema_style") == "name_only")
    check(f"[{mode}] its catalog is smaller than the full-mode one", len(new_catalog) < len(skills.catalog_text()))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
