"""The `stremio` tool group has ONE pack instruction (brief 18, step 1).

Three action files used to declare their own text for the group (stremio_stack.py, stremio_discovery.py,
bulk_download.py); tools.py joins the texts of a group, so the model got all three.  Now one file declares it,
it is not longer than the three old texts were together (every character is prompt tokens), and every rule
the old texts carried is still in it.

Run: python tests/test_hub_stremio_pack_instruction.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-stremio-pack-")
os.environ["USERPROFILE"] = os.environ["HOME"]
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import checklist_schema, tool_registry, tools  # noqa: E402
from jarvis.actions import stremio_stack  # noqa: E402

passed = failed = 0

# Measured on jarvis-main_160_ before this change: 806 (stremio_stack) + 444 (stremio_discovery) + 404 (bulk_download).
OLD_TOTAL = 806 + 444 + 404


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


declarers = sorted(r.file for r in tools._AUTO_VALID if r.group == "stremio" and r.pack_instruction)
check("exactly one file declares an instruction for group stremio", len(declarers) == 1)
check("that file is stremio_stack.py", declarers == ["stremio_stack.py"])
members = sorted(r.file for r in tools._AUTO_VALID if r.group == "stremio")
check("the group still has all its tool files (nothing was dropped from it)",
      {"stremio_stack.py", "stremio_discovery.py", "bulk_download.py", "play_media.py", "stremio_open.py"} <= set(members))

text = tool_registry.pack_instruction("stremio")
check("the merged text is exactly the one declared text", text == stremio_stack.TOOL_PACK_INSTRUCTION)
for name in ("media_find", "media_episodes", "stream_search", "bulk_plan", "bulk_run", "play_episode", "play_local", "stremio_open",
             "stack_status", "stack_start", "stremio_prepare", "stack_doctor", "dl_add", "play_control"):
    check("the text names %s" % name, name in text)
check("brief 22: it says dl_add takes the full path of a .torrent file", ".torrent file" in text)
check("it is not longer than the three old texts together (%d <= %d)" % (len(text), OLD_TOTAL), len(text) <= OLD_TOTAL)

# every rule of the old texts is still in it
check("rule: never invent an id", "never invent" in text)
check("rule: ask the owner when several titles match", "several titles match" in text and "ask" in text)
check("rule: Kitsu ids too, pass id and type exactly as given", "kitsu:N" in text and "exactly as given" in text)
check("rule: a release that doesn't state its language is not a dub", "doesn't state its language is not a dub" in text)
check("rule: bulk_plan queues nothing", "queues nothing" in text)
check("rule: the owner agrees before bulk_run, and bulk_run asks for confirmation itself",
      "only after they agree call bulk_run" in text and "asks for confirmation itself" in text)
check("rule: ask about episodes with no match", "no match" in text)
check("rule: with no season named the plan takes the latest and says so", "latest and says so" in text)
check("rule: the server may already be open in the Stremio app", "already be open in the Stremio app" in text)
check("rule: stremio_prepare asks first", "stremio_prepare (asks first)" in text)
check("play_episode is described as taking a title or an IMDb id", "play_episode takes a title or an IMDb id" in text)

# the order the brief asks for: the stack, finding a show, a whole season, playing
at = [text.find(k) for k in ("stack_status", "Finding a show", "A whole season", "To play")]
check("order: stack, finding a show, a whole season, playing", all(a >= 0 for a in at) and at == sorted(at))

# the two files that used to declare one say where it lives; the two that never did still declare none
for mod in ("stremio_discovery", "bulk_download", "play_media", "stremio_open"):
    src = (ROOT / "jarvis-cli" / "jarvis" / "actions" / (mod + ".py")).read_text(encoding="utf-8")
    check("%s.py has no TOOL_PACK_INSTRUCTION assignment" % mod, "TOOL_PACK_INSTRUCTION =" not in src and "TOOL_PACK_INSTRUCTION=" not in src)

# the group blurb names the new tools and fits the schema
blurb = stremio_stack.TEST_CHECKLIST_GROUP["blurb"]
check("the group blurb names the discovery and bulk tools", all(n in blurb for n in ("media_find", "stream_search", "bulk_plan", "bulk_run", "play_episode")))
check("the group blurb fits the checklist schema (<= %d characters)" % checklist_schema.MAX_BLURB, len(blurb) <= checklist_schema.MAX_BLURB)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
