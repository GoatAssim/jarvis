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


ZIP_SKILL_MD = (
    "---\nname: Blender Render\ndescription: Submit and monitor a render job. "
    "Use for any Blender render request.\n---\n\nSee submit.py.\n"
)


def test_zip_import_flat():
    """SKILL.md at the top of the archive — the shape you get zipping the
    CONTENTS of a skill folder rather than the folder itself."""
    import zipfile
    tmp = fresh_dir()
    try:
        z = tmp / "flat.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("SKILL.md", ZIP_SKILL_MD)
            zf.writestr("REFERENCE.md", "# settings\nsamples=256")
            zf.writestr("submit.py", "print('go')")
        result = skills.add_skill(str(z))
        check("flat zip installs", result.get("added") is True, str(result))
        check("source is reported as zip", result.get("source") == "zip")
        check("catalog picks it up", "Blender Render" in skills.catalog_text())
        loaded = skills.load_skill("Blender Render")
        check("both files listed as references",
              set(loaded.get("references", [])) == {"REFERENCE.md", "submit.py"})
        check("script still refused at read time (tier 3 rule holds for zip-imported skills too)",
              "script" in skills.read_reference("Blender Render", "submit.py").get("error", "").lower())
        check("reference doc still readable",
              "samples" in skills.read_reference("Blender Render", "REFERENCE.md").get("content", ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_zip_import_folder_wrapped_and_junk_filtered():
    """SKILL.md one level down inside the zip's one top folder — the shape
    you get right-clicking a folder and choosing "compress"/"send to zip".
    Also verifies OS junk (.DS_Store, __MACOSX) never reaches the installed
    skill, and that a nested reference (docs/DEEP.md) is still discoverable
    and readable via its relative path."""
    import zipfile
    tmp = fresh_dir()
    try:
        z = tmp / "wrapped.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("render-pack/SKILL.md", ZIP_SKILL_MD)
            zf.writestr("render-pack/docs/DEEP.md", "deep reference content")
            zf.writestr("render-pack/.DS_Store", "junk")
            zf.writestr("__MACOSX/._SKILL.md", "junk")
        result = skills.add_skill(str(z))
        check("wrapped zip installs", result.get("added") is True, str(result))
        folder = skills.SKILLS_DIR / result["slug"]
        check("junk file filtered out", not (folder / ".DS_Store").exists())
        check("__MACOSX never copied in", not any(p.name == "__MACOSX" for p in skills.SKILLS_DIR.iterdir()))
        check("nested reference listed with its relative path",
              "docs/DEEP.md" in skills.list_skills()[0]["references"])
        check("nested reference readable by that path",
              "deep reference" in skills.read_reference("Blender Render", "docs/DEEP.md").get("content", ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_zip_slip_is_blocked():
    """A zip entry can name an arbitrary path ("../../etc/cron.d/x"). This
    must be caught before anything is written to disk, not cleaned up after."""
    import zipfile
    tmp = fresh_dir()
    try:
        z = tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("SKILL.md", "---\nname: Evil\ndescription: an evil skill\n---\n\nbody")
            zf.writestr("../../../tmp/jarvis_zip_slip_probe.txt", "pwned")
        result = skills.add_skill(str(z), name="Evil")
        check("zip slip attempt is rejected", "escapes" in result.get("error", ""), str(result))
        check("no file was actually written outside the archive",
              not Path("/tmp/jarvis_zip_slip_probe.txt").exists())
    finally:
        Path("/tmp/jarvis_zip_slip_probe.txt").unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)


def test_zip_without_skill_md_is_rejected():
    import zipfile
    tmp = fresh_dir()
    try:
        z = tmp / "empty.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("readme.txt", "just a readme")
        result = skills.add_skill(str(z))
        check("zip with no SKILL.md anywhere is rejected", "error" in result)
        check("nothing was installed", skills.list_skills() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_misnamed_non_zip_falls_back_to_markdown_path():
    """A .zip-named file that isn't actually a zip (sniffed by content, not
    extension) must not crash — it should fall through to being treated as
    plain text, and fail there for the ordinary "no description" reason."""
    tmp = fresh_dir()
    try:
        fake = tmp / "not-really.zip"
        fake.write_text("hello world, not a zip", encoding="utf-8")
        result = skills.add_skill(str(fake))
        check("misnamed non-zip doesn't crash", "error" in result)
        check("fails for the ordinary reason, not a zip-parsing error",
              "description" in result["error"].lower())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_zip_duplicate_name_rejected():
    import zipfile
    tmp = fresh_dir()
    try:
        z = tmp / "flat.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("SKILL.md", ZIP_SKILL_MD)
        skills.add_skill(str(z))
        result = skills.add_skill(str(z), name="Blender Render")
        check("duplicate zip import rejected like any other duplicate",
              "already exists" in result.get("error", ""))
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


# --- create_skill: references/ and scripts/ subfolders (module splitting) --

def test_create_skill_with_references_and_scripts():
    tmp = fresh_dir()
    try:
        r = skills.create_skill(
            "Render Pipeline",
            "Submit and monitor Blender render jobs. Use for any render request.",
            "## Overview\nSee references/SETUP.md. Run scripts/submit.py.",
            references={"SETUP.md": "farm=render01", "DEEP.md": "deep"},
            scripts={"submit.py": "print(1)"},
        )
        check("creation reports both subfolders written",
              r.get("created") is True and set(r.get("references", [])) == {"SETUP.md", "DEEP.md"}
              and r.get("scripts") == ["submit.py"])
        folder = skills.SKILLS_DIR / "render-pipeline"
        check("references/ folder actually exists", (folder / "references").is_dir())
        check("scripts/ folder actually exists", (folder / "scripts").is_dir())
        loaded = skills.load_skill("Render Pipeline")
        check("both subfolders' files are discoverable via load_skill",
              set(loaded["references"]) == {"references/SETUP.md", "references/DEEP.md", "scripts/submit.py"})
        check("reference readable by its subpath",
              "render01" in skills.read_reference("Render Pipeline", "references/SETUP.md").get("content", ""))
        check("script still refused at read time regardless of which folder it's in",
              "script" in skills.read_reference("Render Pipeline", "scripts/submit.py").get("error", "").lower())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_skill_rejects_unsafe_module_filenames():
    tmp = fresh_dir()
    try:
        for bad in ("../../etc/passwd", "sub/dir.md", "/etc/passwd", ""):
            out = skills.create_skill("Evil", "an evil skill for testing", "body",
                                      references={bad: "x"})
            check(f"unsafe reference filename rejected: {bad!r}", "error" in out)
        check("nothing was created from the rejected attempts", skills.list_skills() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_skill_without_modules_creates_no_subfolders():
    """A simple skill (the common case) should not sprout empty folders."""
    tmp = fresh_dir()
    try:
        skills.create_skill("Simple", "A simple skill with no modules, for testing.", "just do it")
        folder = skills.SKILLS_DIR / "simple"
        check("no references/ folder for a skill with none",
              not (folder / "references").exists())
        check("no scripts/ folder for a skill with none",
              not (folder / "scripts").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


for t in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    t()

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
