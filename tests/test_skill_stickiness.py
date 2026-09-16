"""Tests for manual skill loading (jarvis/skill_stickiness.py) — the
"/skillload <name>" chat command / `jarvis skillload` CLI backing store.

Run: python tests/test_skill_stickiness.py   (from jarvis-cli/, like the others)

Every test points JARVIS_DIR/STICKY_FILE at a temp path first — none of this
touches the real ~/.jarvis. Also points skills.SKILLS_DIR at a temp dir, since
loaded_context() reads real skills off disk to self-heal stale entries.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import skill_stickiness as ss  # noqa: E402
from jarvis import skills  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_stickiness_test_"))
    ss.JARVIS_DIR = tmp / "jarvis_home"
    ss.STICKY_FILE = ss.JARVIS_DIR / "skill_stickiness.json"
    skills.SKILLS_DIR = tmp / "skills"
    return tmp


def test_empty_store_is_empty():
    tmp = fresh()
    try:
        check("nothing loaded anywhere by default", ss.get_loaded("any-conv") == [])
        check("loaded_context is empty string, not an error", ss.loaded_context("any-conv") == "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_conversation_scope_is_isolated():
    tmp = fresh()
    try:
        ss.load("conv-a", "Skill A")
        check("conv-a sees it", ss.get_loaded("conv-a") == ["Skill A"])
        check("conv-b does not", ss.get_loaded("conv-b") == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_global_scope_reaches_every_conversation():
    tmp = fresh()
    try:
        ss.load(None, "Global Skill")
        check("reaches an arbitrary conversation", "Global Skill" in ss.get_loaded("conv-x"))
        check("reaches another arbitrary conversation too", "Global Skill" in ss.get_loaded("conv-y"))
        check("reaches the CLI's no-conversation case too", "Global Skill" in ss.get_loaded(""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_conversation_and_global_both_apply_deduped():
    tmp = fresh()
    try:
        ss.load(None, "Shared")
        ss.load("conv-1", "Shared")
        ss.load("conv-1", "Only Here")
        loaded = ss.get_loaded("conv-1")
        check("no duplicate when loaded both ways", loaded.count("Shared") == 1)
        check("conversation-specific skill also present", "Only Here" in loaded)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_is_idempotent():
    tmp = fresh()
    try:
        ss.load("conv-1", "X")
        ss.load("conv-1", "X")
        ss.load("conv-1", "X")
        check("loading the same skill repeatedly doesn't duplicate it",
              ss.get_loaded("conv-1") == ["X"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unload_one_leaves_others():
    tmp = fresh()
    try:
        ss.load("conv-1", "A")
        ss.load("conv-1", "B")
        ss.unload("conv-1", "A")
        check("only the unloaded one is gone", ss.get_loaded("conv-1") == ["B"])
        ss.unload("conv-1", "nonexistent")  # must not raise
        check("unloading something never loaded is a harmless no-op",
              ss.get_loaded("conv-1") == ["B"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unload_all_clears_only_its_own_scope():
    tmp = fresh()
    try:
        ss.load("conv-1", "A")
        ss.load(None, "Global")
        ss.unload_all("conv-1")
        check("conv-1's own load is gone", "A" not in ss.get_loaded("conv-1"))
        check("but the global one still reaches it", "Global" in ss.get_loaded("conv-1"))
        ss.unload_all(None)
        check("unload_all(None) clears the global scope",
              ss.get_loaded("conv-1") == [] and ss.get_loaded("conv-2") == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clear_conv_drops_conversation_scope_only():
    """Mirrors route_stickiness.clear_sticky() — a fresh conversation must
    not inherit an old one's force-loaded skill, but a globally-loaded one
    is a deliberate cross-conversation choice and should survive."""
    tmp = fresh()
    try:
        ss.load("conv-1", "Local")
        ss.load(None, "Global")
        ss.clear_conv("conv-1")
        check("conversation-scoped load is cleared", "Local" not in ss.get_loaded("conv-1"))
        check("global load survives clear_conv", "Global" in ss.get_loaded("conv-1"))
        ss.clear_conv(None)  # must not raise on a falsy conv_id
        ss.clear_conv("")
        check("clear_conv on a falsy id is a harmless no-op",
              "Global" in ss.get_loaded("conv-1"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_loaded_context_renders_real_skill_bodies():
    tmp = fresh()
    try:
        skills.create_skill("Weekly Report", "Build the weekly report. For weekly requests.",
                            "## Steps\n1. Pull the board\n")
        ss.load("conv-1", "Weekly Report")
        ctx = ss.loaded_context("conv-1")
        check("context includes the skill's real name", "Weekly Report" in ctx)
        check("context includes the actual instructions", "Pull the board" in ctx)
        check("context is empty for a conversation with nothing loaded",
              ss.loaded_context("conv-2") == "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_loaded_context_self_heals_a_deleted_skill():
    """A skill loaded and then deleted must not haunt every future ask."""
    tmp = fresh()
    try:
        skills.create_skill("Temp", "A temporary skill, for testing self-healing.", "body")
        ss.load("conv-1", "Temp")
        ss.load(None, "Temp")
        check("loaded before deletion", "Temp" in ss.get_loaded("conv-1"))
        skills.remove_skill("Temp")
        ctx = ss.loaded_context("conv-1")
        check("stale skill produces no error text leaking into the prompt",
              "error" not in ctx.lower())
        check("stale skill silently absent from the rendered context", "Temp" not in ctx)
        check("stale entry is cleared from conversation scope", "Temp" not in ss.get_loaded("conv-1"))
        check("stale entry is cleared from global scope too", "Temp" not in ss.get_loaded("conv-2"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_falsy_inputs_never_raise():
    tmp = fresh()
    try:
        ss.load("", "")
        ss.load(None, None)
        ss.unload("", "")
        check("empty/None calls are all safely absorbed", ss.get_loaded("") == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


for t in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    t()

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
