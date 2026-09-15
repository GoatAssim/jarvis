"""Tests for the skills system (jarvis/skills.py) and the hybrid catalog
tier (ai_client's tier-1/tier-2 split of the router's tool offering).

Run: python tests/test_skills.py   (from jarvis-cli/, like the others)

Every test points SKILLS_DIR at a temp directory first — none of this ever
touches the real ~/.jarvis/skills.

The properties that matter:
  - tier 1 stays cheap (one line per skill, no bodies)
  - tier 2 loads only on request
  - tier 3 refuses to READ scripts, because "scripts are run, not read" is
    the entire reason a skill can ship megabytes for ~0 token cost
  - a skill with no description is rejected at write time rather than
    installed-but-permanently-undiscoverable
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import skills  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh_dir():
    """Point the store at a throwaway directory. Returns the path."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_skills_test_"))
    skills.SKILLS_DIR = tmp / "skills"
    return tmp


DESC = "Build the Monday status report from the sprint board. Use when asked for the weekly report."
BODY = "## Steps\n1. Pull the board\n2. Group by owner\n"


def seed():
    tmp = fresh_dir()
    skills.create_skill("Weekly Report", DESC, BODY, ["report", "weekly"])
    return tmp


# --- frontmatter -----------------------------------------------------------

def test_frontmatter_parsing():
    meta, body = skills.parse_frontmatter(
        "---\nname: X\ndescription: does a thing\nkeywords: [a, b]\n---\n\nBODY HERE")
    check("name/description parsed", meta.get("name") == "X" and meta["description"] == "does a thing")
    check("inline list parsed", meta.get("keywords") == ["a", "b"])
    check("body separated", body.strip() == "BODY HERE")


def test_malformed_frontmatter_does_not_raise():
    meta, body = skills.parse_frontmatter("no frontmatter at all")
    check("missing frontmatter degrades to ({}, text)", meta == {} and body == "no frontmatter at all")
    meta2, _ = skills.parse_frontmatter("---\nbroken")
    check("half-open frontmatter degrades quietly", meta2 == {})


# --- tier 1 ----------------------------------------------------------------

def test_empty_store_costs_nothing():
    tmp = fresh_dir()
    try:
        check("no skills = empty catalog (zero tokens for the feature)",
              skills.catalog_text() == "" and skills.list_skills() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_catalog_is_names_and_descriptions_only():
    tmp = seed()
    try:
        catalog = skills.catalog_text()
        check("catalog names the skill", "Weekly Report" in catalog)
        check("catalog carries the description", "sprint board" in catalog)
        check("catalog does NOT carry the body (that's the whole point)",
              "Pull the board" not in catalog)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_long_description_is_clipped_in_catalog_only():
    tmp = fresh_dir()
    try:
        long_desc = "w" * (skills.DESC_MAX + 200)
        skills.create_skill("Chatty", long_desc, "body")
        check("catalog clips a runaway description",
              len(skills.catalog_text()) < skills.DESC_MAX + 300)
        check("the file itself keeps the full text",
              long_desc in skills.export_skill("Chatty")["content"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- tier 2 ----------------------------------------------------------------

def test_load_returns_body_and_lists_but_does_not_load_references():
    tmp = seed()
    try:
        folder = skills.SKILLS_DIR / "weekly-report"
        (folder / "FORMAT.md").write_text("# Format\nBullets only.", encoding="utf-8")
        loaded = skills.load_skill("Weekly Report")
        check("tier 2 returns the instructions", "Pull the board" in loaded["instructions"])
        check("references are advertised", loaded.get("references") == ["FORMAT.md"])
        check("but their contents are NOT loaded",
              "Bullets only" not in str(loaded))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_name_resolution_is_forgiving():
    tmp = seed()
    try:
        for probe in ("Weekly Report", "weekly report", "weekly-report", "Weekly"):
            check(f"resolves {probe!r}", skills.load_skill(probe).get("loaded") is True)
        check("unknown name errors with the installed list",
              "No skill named" in skills.load_skill("nonsense").get("error", ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- tier 3 ----------------------------------------------------------------

def test_reference_read_and_script_refusal():
    tmp = seed()
    try:
        folder = skills.SKILLS_DIR / "weekly-report"
        (folder / "FORMAT.md").write_text("# Format\nBullets only.", encoding="utf-8")
        (folder / "push.py").write_text("print('hi')", encoding="utf-8")
        ok = skills.read_reference("Weekly Report", "FORMAT.md")
        check("tier 3 reads a document on request", "Bullets only" in ok.get("content", ""))
        refused = skills.read_reference("Weekly Report", "push.py")
        check("tier 3 REFUSES to read a script", "script" in refused.get("error", "").lower())
        check("and says to run it instead", "run it" in refused.get("hint", "").lower())
        check("script source never enters the reply",
              "print('hi')" not in str(refused))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reference_path_traversal_is_blocked():
    """A reference filename arrives from the model, so "../../.ssh/id_rsa"
    is a request that will eventually be made."""
    tmp = seed()
    try:
        skills.create_skill("Other Skill", "A second skill, for escape tests.", "secret body")
        for probe in ("../../../etc/passwd", "/etc/passwd",
                      "../other-skill/SKILL.md", "..\\..\\windows\\win.ini"):
            out = skills.read_reference("Weekly Report", probe)
            check(f"escape blocked: {probe}", "error" in out, str(out)[:70])
        # A path that normalizes back INSIDE its own folder is contained, so
        # it is allowed — and harmless, since load_skill already returns
        # SKILL.md. Asserted explicitly so nobody "fixes" containment into
        # string-matching on ".." and calls that a security improvement.
        inside = skills.read_reference("Weekly Report", "../weekly-report/SKILL.md")
        check("a path normalizing back into the same folder is allowed",
              "content" in inside, str(inside)[:70])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- management ------------------------------------------------------------

def test_description_is_mandatory():
    tmp = fresh_dir()
    try:
        out = skills.create_skill("No Desc", "", "body")
        check("a skill with no description is rejected", "error" in out)
        check("rejection explains why it matters",
              "description" in out["error"].lower())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_duplicate_is_rejected_not_silently_overwritten():
    tmp = seed()
    try:
        out = skills.create_skill("Weekly Report", DESC, "different body")
        check("duplicate rejected", "already exists" in out.get("error", ""))
        check("original body intact",
              "Pull the board" in skills.load_skill("Weekly Report")["instructions"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_add_from_markdown_and_remove():
    tmp = fresh_dir()
    try:
        md = "---\nname: Pasted\ndescription: A pasted skill, used when testing.\n---\n\nDo the thing."
        added = skills.add_skill(md)
        check("add from pasted markdown works", added.get("added") is True, str(added))
        check("it shows up in the catalog", "Pasted" in skills.catalog_text())
        no_desc = skills.add_skill("---\nname: Bad\n---\n\nbody")
        check("markdown with no description is rejected", "error" in no_desc)
        check("remove works", skills.remove_skill("Pasted").get("removed") is True)
        check("and it's gone from the catalog", "Pasted" not in skills.catalog_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_rejects_an_edit_that_would_break_discovery():
    tmp = seed()
    try:
        bad = skills.write_skill_file("Weekly Report", "---\nname: Weekly Report\n---\n\nbody")
        check("saving a description-less edit is refused", "error" in bad)
        good = skills.write_skill_file(
            "Weekly Report", f"---\nname: Weekly Report\ndescription: {DESC}\n---\n\nnew body")
        check("a valid edit saves", good.get("saved") is True)
        check("and takes effect", "new body" in skills.load_skill("Weekly Report")["instructions"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_broken_skill_is_visible_not_silent():
    """A skill the user thinks they installed but that never loads is the
    worst failure mode this feature could have."""
    tmp = fresh_dir()
    try:
        (skills.SKILLS_DIR / "empty-folder").mkdir(parents=True)
        listed = skills.list_skills()
        check("a folder with no SKILL.md is listed as invalid",
              len(listed) == 1 and listed[0]["valid"] is False)
        check("with a reason attached", bool(listed[0].get("error")))
        check("but is kept out of the prompt catalog", skills.catalog_text() == "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- tools surface ---------------------------------------------------------

def test_tool_schemas_are_registered_and_grouped():
    from jarvis import tool_registry
    from jarvis import tools as system_tools

    expected = {"list_skills", "load_skill", "load_skill_reference",
                "create_skill", "add_skill", "remove_skill"}
    check("every skill tool is callable", expected <= set(system_tools.TOOLS))
    check("every skill tool has a schema", expected <= set(tool_registry.TOOL_INDEX))
    check("they share the 'skills' router group",
          set(tool_registry.tools_in_group("skills")) == expected)
    check("remove_skill is confirm-gated",
          "remove_skill" in __import__("jarvis.tool_safety", fromlist=["x"]).DEFAULT_CONFIRM_REQUIRED)


# --- hybrid catalog tier ---------------------------------------------------

def test_catalog_tier_is_cheaper_and_still_reachable():
    from jarvis import token_usage
    from jarvis import tool_router
    from jarvis import tools as system_tools

    route = tool_router.route("launch a game on playnite")
    full = system_tools.schemas_for_tools(route.tools)
    check("playnite is a big group (this test is meaningless otherwise)", len(full) > 10)

    hot = {n for _g, n, _p in route.matches}
    cold = [s for s in full if s["name"] not in hot]
    compact_cost = token_usage.estimate_tokens_for(system_tools.compact_schemas_for_prompt(full))
    hybrid_cost = (
        token_usage.estimate_tokens_for(
            system_tools.compact_schemas_for_prompt([s for s in full if s["name"] in hot]))
        + token_usage.estimate_tokens_for(system_tools.catalog_schemas_for_prompt(cold))
    )
    check(f"hybrid tier is cheaper ({compact_cost} -> {hybrid_cost} tok)",
          hybrid_cost < compact_cost)

    entries = system_tools.catalog_schemas_for_prompt(cold)
    check("catalog entries carry no argument schema",
          all(not e["parameters"].get("properties") for e in entries))
    check("and tell the model how to get one",
          all("get_tool_schema" in e["description"] for e in entries))


def test_get_tool_schema_round_trip():
    from jarvis import tools as system_tools

    got = system_tools.tool_get_tool_schema({"name": "playnite_launch_game"})
    check("get_tool_schema returns a real schema", got.get("ready") is True, str(got)[:80])
    check("with arguments attached", "parameters" in got.get("schema", {}))
    miss = system_tools.tool_get_tool_schema({"name": "playnite_launch"})
    check("a near-miss suggests real names instead of just failing",
          bool(miss.get("did_you_mean")), str(miss)[:80])
    check("an empty name is handled", "error" in system_tools.tool_get_tool_schema({}))


for t in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    t()

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
