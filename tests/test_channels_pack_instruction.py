"""The shared "channels" tool group keeps EVERY file's pack instruction.

    python3 tests/test_channels_pack_instruction.py

Five action files join the "channels" group and each used to declare its own
TOOL_PACK_INSTRUCTION, but tools.AUTO_TOOL_PACK_INSTRUCTIONS was a plain dict, so only the
last discovered file's text reached the model: the remember_sender, recent_dms and
notify_owner guidance, and the "don't call recent_dms first" rule for send_dm, were
silently dropped. Now the texts of a group are joined (distinct ones, discovery order), the
copies that only competed were removed, and a guest is not told about owner-only tools.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-packinstr-")
os.environ["USERPROFILE"] = os.environ["HOME"]
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_registry, tools  # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
    else:
        FAIL.append(name + (" - " + detail if detail else ""))


full = tool_registry.pack_instruction("channels")
check("remember_sender guidance reaches the model", "remember_sender" in full and "ask what to call" in full)
check("recent_dms guidance reaches the model", "recent_dms is read-only and owner-only" in full)
check("send_dm guidance, incl. 'do not call recent_dms first' and `when`, reaches the model",
      "send_dm messages known people" in full and "don't call recent_dms first" in full and "pass `when`" in full)
check("recent_spaces guidance reaches the model", "recent_spaces is read-only and owner-only" in full)
check("notify_owner guidance reaches the model", "notify_owner DMs the owner" in full)
check("no sentence is repeated", len(re.findall(r"recent_dms is read-only", full)) == 1
      and len(re.findall(r"notify_owner DMs the owner", full)) == 1 and len(re.findall(r"send_dm messages known", full)) == 1)

# Every declaring file's text is in the merged one (so a new file's guidance cannot be dropped).
declared = [(r.file, r.pack_instruction) for r in tools._AUTO_VALID if r.group == "channels" and r.pack_instruction]
check("every file in the group that declares one is included", declared and all(text in full for _f, text in declared), str(declared)[:200])
positions = [full.find(t) for _f, t in declared]
check("the order is the discovery order", all(p >= 0 for p in positions) and positions == sorted(positions), str(positions))

# The one-text-per-group rule for the files that share their guidance in channel_people.py.
only_people = {f for f, _t in declared}
check("only channel_people.py and notify_owner.py declare an instruction for the group (the others had competing copies)",
      only_people == {"channel_people.py", "notify_owner.py"}, str(sorted(only_people)))

# A group with a single declarer is untouched by the merge.
others = {}
for r in tools._AUTO_VALID:
    if r.pack_instruction and r.group != "channels":
        others.setdefault(r.group, []).append(r.pack_instruction)
check("every other group still gets exactly its own instruction",
      all(tool_registry.pack_instruction(g) == (texts[0] if len(texts) == 1 else tool_registry.pack_instruction(g))
          for g, texts in others.items()) and all(len(t) == 1 for t in others.values()))

# Guests: no owner-only tool named; the two sentences are verbatim parts of the full text.
guest = tool_registry.pack_instruction("channels", {"send_dm", "recent_dms", "recent_spaces"})
check("a guest's instruction names no owner-only tool", not any(n in guest for n in ("send_dm", "recent_dms", "recent_spaces")))
check("a guest still gets remember_sender and notify_owner", "remember_sender" in guest and "notify_owner" in guest)
check("the guest text is made only of verbatim parts of the full text",
      all(part in full for part in guest.split(". ")[:1]) and guest.split("notify_owner DMs")[0].strip() in full
      and ("notify_owner DMs" + guest.split("notify_owner DMs")[1]) in full)
for name in ("send_dm", "recent_dms", "recent_spaces"):
    check(f"missing {name} alone also gives the guest text", tool_registry.pack_instruction("channels", {name}) == guest)
check("an unrelated unoffered tool changes nothing", tool_registry.pack_instruction("channels", {"take_screenshot"}) == full)
check("the vision variant is unchanged", tool_registry.pack_instruction("vision", {"take_screenshot"}).startswith("look_at_screen reads the screen text first"))

print(f"{PASS}/{PASS + len(FAIL)} passed")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
