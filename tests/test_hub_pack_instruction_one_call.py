"""The `stremio` pack instruction after round 8 (N32, brief 8D): it describes the one-call play flow, in one text, and it did not
grow by more than two sentences.  A new file on purpose: test_hub_stremio_pack_instruction.py and test_hub_stremio_mpv.py pin the
old length ceiling (806 + 444 + 404 = 1654 characters), and the new text fits under it, so neither had to change.

Run: python tests/test_hub_pack_instruction_one_call.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-pack-one-call-")
os.environ["USERPROFILE"] = os.environ["HOME"]
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_registry, tools  # noqa: E402
from jarvis.actions import stremio_stack  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


TEXT_175 = 1613            # the length of the text in jarvis-main_175_
TWO_SENTENCES = 160
text = tool_registry.pack_instruction("stremio")
check("it is still the one text declared by stremio_stack.py", text == stremio_stack.TOOL_PACK_INSTRUCTION
      and sorted(r.file for r in tools._AUTO_VALID if r.group == "stremio" and r.pack_instruction) == ["stremio_stack.py"])
check("it is not longer than the _175_ text plus two sentences (%d <= %d)" % (len(text), TEXT_175 + TWO_SENTENCES), len(text) <= TEXT_175 + TWO_SENTENCES)
check("and it still fits under the old 1654-character ceiling that two other tests pin (%d)" % len(text), len(text) <= 806 + 444 + 404)
check("play with ONE play_episode call: title or IMDb id, season and episode, quality, audio and language",
      "ONE play_episode call" in text and "play_episode takes a title or an IMDb id" in text
      and all(w in text for w in ("quality", "audio", "language")))
check("it says the call finds, ranks and tries the releases itself", "finds, ranks and tries releases itself" in text)
check("after a stream_search, pass pick = the row's rank", "After stream_search pass pick = the row's rank" in text)
check("'stream' or 'watch' is play now; 'download' is dl_add", '"Stream"/"watch" = play now' in text and '"download" = dl_add' in text)
check("the owner is asked only when a tool says so", "Ask the owner only when a tool says so" in text)
check("the old three-step description of playing ('stream list in stremio-web', 'plus season and episode for a series') is gone",
      "stream list in stremio-web" not in text and "plus season and episode for a series" not in text)
check("the other rules are still there (a sample)", all(k in text for k in ("never invent", "queues nothing", "only after they agree call bulk_run", ".torrent file", "play_control", "play_local")))
check("it does not name an argument of dl_add that belongs to a source (rule from N4)", "dl_add" in text and "dl_add_ytdlp" not in text and "dl_add_stremio" not in text)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
