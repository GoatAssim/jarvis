"""actions/minecraft_packs.py: the tool `make_modpack_file`, end to end against an in-process fake adapter (agent P5).

Covers (plan scenarios 3, 4, 8 and 9 for this tool):
  * registration: TOOL_GROUP "minecraft", keywords of weight 5 or more, confirm-gated by name, NO second pack instruction and NO
    setting declared here (both live in minecraft_lookup.py), a Test Checklist entry of its own section `minecraft_packs` that the
    schema accepts and discovery took, result shaping that keeps the untrusted note and the verdict;
  * a guest (and an unreadable sender) is refused BEFORE anything is planned;
  * `dry_run`: reports, asks nothing, writes nothing, creates nothing (also not a new folder);
  * a real run asks "Save outside the downloads library?" (default no) with the folder and the src folder in the text; "no" writes
    nothing; "yes" writes ONE file and says written, never installed; a refused destination never asks;
  * a clarifying question and a failure pass through; a tool never raises; no key or secret in anything returned.

No network.  Run: python tests/test_minecraft_packs_tool.py
"""
import copy
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

HOME = os.path.realpath(tempfile.mkdtemp(prefix="jarvis-packs-tool-"))
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER",
             "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import checklist_schema, tool_result_shaping, tool_safety, tools, ui_bridge  # noqa: E402
from jarvis.actions import minecraft_packs as mp  # noqa: E402
from jarvis.modhosts import packs, sources  # noqa: E402

tools._tool_index()
DATA = ROOT / "tests" / "data" / "modhosts"
CATALOG = json.loads((DATA / "hand_p5_catalog.json").read_text(encoding="utf-8"))
PASS, FAIL = [], []
FAKE_KEY = "FAKE-KEY-0123456789abcdef"
GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
COUNTER = [0]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else (name, str(detail)[:300]))
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else "  -> " + str(detail)[:300]))


class FakeAdapter:
    def __init__(self, platform):
        block = CATALOG[platform]
        self.projects = {p["id"]: copy.deepcopy(p) for p in block["projects"]}
        self.files = copy.deepcopy(block["files"])
        self.calls = []

    def _find(self, ref):
        text = str(ref).strip()
        if text in self.projects:
            return self.projects[text]
        return next((p for p in self.projects.values() if p["slug"].lower() == text.lower()), None)

    def search(self, query, kind="mod", **kw):
        self.calls.append(("search", query))
        norm = lambda s: re.sub(r"[^0-9a-z]+", "", str(s).lower())  # noqa: E731
        hits = [p for p in self.projects.values() if norm(query) and norm(query) in norm(p["name"])]
        return {"ok": True, "hits": hits[:10], "total": len(hits)}

    def get_project(self, ref):
        self.calls.append(("get_project", ref))
        p = self._find(ref)
        return {"ok": True, "project": p} if p else {"ok": False, "reason": "not_found", "error": "No such project."}

    def bulk_projects(self, refs):
        self.calls.append(("bulk_projects", list(refs)))
        return {"ok": True, "projects": {r: self._find(r) for r in refs}}

    def bulk_list_files(self, ids, mc_version=None, loader=None):
        self.calls.append(("bulk_list_files", list(ids)))
        return {"ok": True, "files": {p: [f for f in self.files.get(str(p), []) if (mc_version is None or mc_version in f["mc_versions"])
                                          and (loader is None or loader in f["loaders"])] for p in ids}}

    def bulk_get_files(self, pairs):
        self.calls.append(("bulk_get_files", list(pairs)))
        return {"ok": True, "files": {"%s:%s" % (p, f): next((x for x in self.files.get(str(p), []) if x["file_id"] == str(f)), None) for p, f in pairs}}


class World:
    """Fake adapters + a recording ui.confirm (answers `answer`)."""

    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    def __enter__(self):
        self.adapters = {"modrinth": FakeAdapter("modrinth"), "curseforge": FakeAdapter("curseforge")}
        self.real_get, self.real_confirm = sources.get, ui_bridge.confirm
        adapters = self.adapters
        sources.get = lambda name: adapters.get(str(name).strip().lower())

        def confirm(question, body="", level="warn", confirm_label="Yes", cancel_label="No", default=False, timeout=None):
            self.asked.append({"question": question, "body": body, "default": default})
            return self.answer
        ui_bridge.confirm = confirm
        return self

    def __exit__(self, *exc):
        sources.get, ui_bridge.confirm = self.real_get, self.real_confirm


class Sender:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("JARVIS_CHANNEL_SENDER")
        if self.value is None:
            os.environ.pop("JARVIS_CHANNEL_SENDER", None)
        else:
            os.environ["JARVIS_CHANNEL_SENDER"] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("JARVIS_CHANNEL_SENDER", None)
        else:
            os.environ["JARVIS_CHANNEL_SENDER"] = self.old


def new_dir(label="d"):
    COUNTER[0] += 1
    path = Path(HOME) / ("%s%d" % (label, COUNTER[0]))
    path.mkdir(parents=True)
    return path


def args(fmt="modrinth", **over):
    base = {"format": fmt, "name": "Tool Pack", "mc_version": "1.20.1", "loader": "fabric", "loader_version": "0.15.11",
            "entries": [{"project": "sodium"}, {"project": "lithium"}], "dest": str(new_dir("out"))}
    base.update(over)
    return base


def listing(folder):
    return sorted(p.name for p in Path(folder).iterdir()) if Path(folder).exists() else None


# --------------------------------------------------------------------------- registration

def test_registration_and_contract():
    check("the tool is loaded", "make_modpack_file" in tools._tool_index())
    check("group is minecraft", mp.TOOL_GROUP == "minecraft" and "make_modpack_file" in tools.AUTO_TOOL_GROUPS.get("minecraft", []), tools.AUTO_TOOL_GROUPS.get("minecraft"))
    check("keywords: weight 5 or more, discovered", set(mp.TOOL_KEYWORDS) == {"make_modpack_file"} and mp.TOOL_KEYWORDS["make_modpack_file"]
          and all(isinstance(w, int) and w >= 5 for w in mp.TOOL_KEYWORDS["make_modpack_file"].values()) and "make_modpack_file" in tools.AUTO_TOOL_KEYWORDS)
    check("confirm-gated by name", mp.TOOL_CONFIRM_REQUIRED == {"make_modpack_file"} and "make_modpack_file" in tool_safety.DEFAULT_CONFIRM_REQUIRED)
    check("no second pack instruction and no setting declared in this file (they live in minecraft_lookup.py)",
          not hasattr(mp, "TOOL_PACK_INSTRUCTION") and not hasattr(mp, "TOOL_SETTINGS"))
    check("the group's ONE pack instruction names this tool and says dry_run first",
          "make_modpack_file" in tools.AUTO_TOOL_PACK_INSTRUCTIONS.get("minecraft", "") and "dry_run" in tools.AUTO_TOOL_PACK_INSTRUCTIONS.get("minecraft", ""))
    check("the name does not say install", "install" not in "make_modpack_file")
    schema = mp.TOOL_SCHEMAS[0]
    props = schema["parameters"]["properties"]
    check("schema: required fields", schema["parameters"]["required"] == ["format", "name", "mc_version", "entries"])
    check("schema: loader_version and dest are OPTIONAL", "loader_version" not in schema["parameters"]["required"] and "dest" not in schema["parameters"]["required"])
    check("schema: every name of the plan is there", {"format", "name", "version", "author", "summary", "mc_version", "loader", "loader_version", "entries", "src",
                                                       "include_dependencies", "dest", "filename", "overwrite", "dry_run"} <= set(props), sorted(props))
    check("schema: the description says dry_run first and never 'installed'", "dry_run" in schema["description"] and "never 'installed'" in schema["description"])
    check("schema: no oneOf/anyOf (providers differ on them)", "anyOf" not in json.dumps(schema) and "oneOf" not in json.dumps(schema))
    check("schema enums match the code", props["format"]["enum"] == list(packs.FORMATS) and props["channel"]["enum"] == list(packs.CHANNELS))
    check("the discovered schema is the same", any(s["name"] == "make_modpack_file" for s in tools.AUTO_TOOL_SCHEMAS))
    if "make_modpack_file" in tools.OWNER_ONLY_TOOLS:
        with Sender(GUEST):
            check("a guest is not offered it", "make_modpack_file" in tools.hidden_from_sender())
    else:
        check("(pending the merge edit) make_modpack_file is not in OWNER_ONLY_TOOLS yet: see 'For the merge'; the handler refuses a guest itself", True)


def test_checklist_entry_is_well_formed_and_discovered():
    check("one entry for the tool", set(mp.TEST_CHECKLIST) == {"make_modpack_file"})
    entry = mp.TEST_CHECKLIST["make_modpack_file"]
    problems = checklist_schema.validate_entry("make_modpack_file", entry)
    check("it passes checklist_schema", problems == [], problems)
    check("own section id minecraft_packs, declared here", entry["group"] == "minecraft_packs" and "minecraft_packs" in mp.TEST_CHECKLIST_GROUP)
    meta = mp.TEST_CHECKLIST_GROUP["minecraft_packs"]
    check("section label and blurb pass the schema", meta["label"] == "Minecraft: packs" and checklist_schema.validate_group_meta(meta) == [], checklist_schema.validate_group_meta(meta))
    check("discovery accepted entry and section", "make_modpack_file" in tools.AUTO_TEST_CHECKLIST and "minecraft_packs" in tools.AUTO_TEST_CHECKLIST_GROUPS)
    check("the entry has a `care` line (it writes a file and reads a folder)", "care" in entry and "src" in entry["care"])
    runs = [s["run"] for s in entry["steps"] if "run" in s]
    props = set(mp.TOOL_SCHEMAS[0]["parameters"]["properties"])
    check("every `run` step uses only schema arguments", all(set(r) <= props for r in runs), [set(r) - props for r in runs])
    check("the first step is a dry run", entry["steps"][0].get("run", {}).get("dry_run") is True)
    check("the plan's two refusal checks are there (~/.jarvis as dest and as src)",
          any(r.get("dest") == "~/.jarvis" for r in runs) and any(r.get("src") == "~/.jarvis" for r in runs))
    seen = [s.get("ask") or json.dumps(s.get("run"), sort_keys=True) for s in entry["steps"]]
    check("no duplicate step", len(seen) == len(set(seen)))


def test_result_shaping_keeps_the_verdict():
    with World():
        res = mp.tool_make_modpack_file(args(dry_run=True))
    check("a spec is registered for the tool", "make_modpack_file" in tool_result_shaping.TOOL_RESULT_SPECS)
    for level in ("medium", "low"):
        shaped = tool_result_shaping.shape_result("make_modpack_file", res, level)
        check("%s: the untrusted note, the verdict and the report lists survive" % level,
              shaped["untrusted_note"] == res["untrusted_note"] and shaped["ok"] is True and shaped["dry_run"] is True and shaped["written"] is False
              and shaped["installed"] is False and shaped["path"] == res["path"] and shaped["message"] == res["message"]
              and len(shaped["resolved"]) == len(res["resolved"]) and "dependencies_added" in shaped and "skipped" in shaped, sorted(shaped))
        check("%s: item names and file names stay" % level, all("project" in r and "file_name" in r for r in shaped["resolved"]))
    fake = {"ok": True, "message": "x", "resolved": [], "dependencies_added": [],
            "skipped": [{"project": "p", "reason": "not_distributable", "page_url": "https://www.curseforge.com/minecraft/mc-mods/p", "slug": "p"}]}
    check("a blocked file keeps its project page at every level", all(tool_result_shaping.shape_result("make_modpack_file", fake, lv)["skipped"][0].get("page_url") for lv in ("medium", "low")))
    check("the original was not changed by shaping", "slug" in res["resolved"][0] and "counts" in res)


# --------------------------------------------------------------------------- guests

def test_a_guest_is_refused_before_anything_is_planned():
    out = new_dir("out")
    real = packs.plan_pack
    packs.plan_pack = lambda a: (_ for _ in ()).throw(AssertionError("planned for a guest"))
    try:
        with World() as w:
            for label, value in (("a chat guest", GUEST), ("unreadable JSON", "{not json"), ("a JSON list", "[1]"),
                                 ("a deeply nested value", "[" * 5000 + "]" * 5000)):
                with Sender(value):
                    res = mp.tool_make_modpack_file(args(dest=str(out)))
                check("%s is refused" % label, res["ok"] is False and res["reason"] == "unsupported" and "owner's" in res["error"], res)
            check("nothing was asked, resolved or written", not w.asked and not any(a.calls for a in w.adapters.values()) and listing(out) == [])
    finally:
        packs.plan_pack = real
    with World():
        with Sender(OWNER):
            check("the owner is not refused", mp.tool_make_modpack_file(args(dry_run=True))["ok"] is True)
        with Sender(None):
            check("no chat context (the PC, a scheduled job) is the owner's", mp.tool_make_modpack_file(args(dry_run=True))["ok"] is True)
        with Sender(json.dumps({"platform": "", "user_id": ""})):
            check("a sender record with no platform/user is no chat guest (handler convention)", mp.tool_make_modpack_file(args(dry_run=True))["ok"] is True)


# --------------------------------------------------------------------------- dry run

def test_dry_run_reports_and_writes_nothing():
    parent = new_dir("parent")
    fresh = parent / "NotYet"
    src = new_dir("src")
    (src / "options.txt").write_text("fov:90\n")
    with World() as w:
        res = mp.tool_make_modpack_file(args(dest=str(fresh), dry_run=True, src=str(src)))
        check("ok, dry_run, not written, not installed", res["ok"] and res["dry_run"] is True and res["written"] is False and res["installed"] is False, res)
        check("the report: resolved, dependencies_added, skipped, overrides, loader version", [r["project"] for r in res["resolved"]] == ["Sodium", "Lithium"]
              and [d["project"] for d in res["dependencies_added"]] == ["Fabric API"] and res["skipped"] == [] and res["overrides"]["files"] == 1
              and res["loader_version"] == "0.15.11" and res["loader_version_source"] == "given", res)
        check("the path is the one the real run would write", res["path"] == str(fresh / "Tool Pack-1.0.0.mrpack"))
        check("a dry run asks NOTHING (the framework gate asks per call)", w.asked == [])
        check("a dry run creates nothing, not even the new folder", not fresh.exists())
        check("dry_run given as a string 'true' works", mp.tool_make_modpack_file(args(dest=str(fresh), dry_run="true"))["dry_run"] is True and not fresh.exists())
        check("the message says nothing was written", "Nothing was written" in res["message"])
        res = mp.tool_make_modpack_file(args("curseforge", dest=str(fresh), dry_run=True))
        check("CurseForge dry run works the same", res["ok"] and res["dry_run"] is True and not fresh.exists(), res)


# --------------------------------------------------------------------------- the real run

def test_real_run_asks_the_outside_library_question():
    src = new_dir("src")
    (src / "options.txt").write_text("fov:90\n")
    out = new_dir("out")
    with World(answer=False) as w:
        res = mp.tool_make_modpack_file(args(dest=str(out), src=str(src)))
        check("declined: nothing written, cancelled, a plain reason", res["ok"] is False and res["reason"] == "bad_dest" and res.get("cancelled") is True and listing(out) == [], (res, listing(out)))
        q = w.asked[0]
        check("the question is the hub's, default NO", q["question"] == "Save outside the downloads library?" and q["default"] is False)
        check("its text shows the folder, the file, the counts and the src folder", str(out) in q["body"] and "Tool Pack-1.0.0.mrpack" in q["body"]
              and str(src) in q["body"] and "3 project files" in q["body"] and "1 file" in q["body"], q["body"])
    with World(answer=True) as w:
        res = mp.tool_make_modpack_file(args(dest=str(out), src=str(src)))
        check("confirmed: ONE file written, no leftovers", res["ok"] and res["written"] is True and listing(out) == ["Tool Pack-1.0.0.mrpack"], (res, listing(out)))
        check("asked exactly once", len(w.asked) == 1)
        check("says written, never installed", res["installed"] is False and "Written only" in res["message"] and "nothing was installed" in res["message"])
        with zipfile.ZipFile(res["path"]) as zf:
            check("the file is a real pack", "modrinth.index.json" in zf.namelist() and "overrides/options.txt" in zf.namelist())
        again = mp.tool_make_modpack_file(args(dest=str(out)))
        check("the same name again: exists (asks nothing, nothing replaced)", again["reason"] == "exists" and len(w.asked) == 1 and listing(out) == ["Tool Pack-1.0.0.mrpack"], again)
        over = mp.tool_make_modpack_file(args(dest=str(out), overwrite=True, summary="changed"))
        check("overwrite=true replaces it (asks again)", over["ok"] and len(w.asked) == 2 and listing(out) == ["Tool Pack-1.0.0.mrpack"], over)
        res = mp.tool_make_modpack_file(args("curseforge", dest=str(out)))
        check("a CurseForge pack: one .zip with manifest.json", res["ok"] and res["path"].endswith(".zip") and zipfile.ZipFile(res["path"]).namelist() == ["manifest.json"], res)


def test_refusals_never_ask():
    with World() as w:
        for label, over in (("~/.jarvis as dest", {"dest": str(Path(HOME) / ".jarvis")}), ("the home folder", {"dest": HOME}),
                            ("a drive root", {"dest": Path.cwd().anchor}), ("a relative dest", {"dest": "some/folder"}),
                            ("~/.jarvis as src", {"src": str(Path(HOME) / ".jarvis")}), ("a src that does not exist", {"src": str(Path(HOME) / "nope")}),
                            ("an unknown loader", {"loader": "rift"}), ("a bad Minecraft version", {"mc_version": "latest"})):
            res = mp.tool_make_modpack_file(args(**over))
            want = "bad_dest" if ("dest" in over or "src" in over) else "unsupported"
            check("%s: refused with %s, nothing asked" % (label, want), res["ok"] is False and res["reason"] == want and not w.asked, res)
        out = new_dir("out")
        check("nothing was written anywhere", listing(out) == [])


def test_questions_and_failures_pass_through():
    with World() as w:
        for missing in ("name", "mc_version", "loader", "entries"):
            a = args()
            a[missing] = None if missing != "entries" else []
            res = mp.tool_make_modpack_file(a)
            check("missing %s: a question for the owner, nothing asked or written" % missing, res["ok"] is True and res["needs_clarification"] is True and res["question"] and not w.asked, res)
        res = mp.tool_make_modpack_file(args(entries=["nothing-by-this-name-xyz"], dry_run=True))
        check("nothing resolves: no_compatible_file with the skipped list (dry run too)", res["ok"] is False and res["reason"] == "no_compatible_file" and res["skipped"][0]["reason"] == "not_found", res)
        res = mp.tool_make_modpack_file(args(entries=["Dup Mod", "lithium"], dry_run=True))
        check("an ambiguous name is listed with candidates, not guessed", [s["reason"] for s in res["skipped"]] == ["ambiguous"] and len(res["skipped"][0]["candidates"]) == 2 and res["complete"] is False, res)
    out = new_dir("out")
    with World(answer=True) as w:
        w.adapters["curseforge"] = None
        res = mp.tool_make_modpack_file(args("curseforge", dest=str(out)))
        check("CurseForge not installed: one sentence, nothing written", res["ok"] is False and res["reason"] == "unsupported" and listing(out) == [], res)


def test_a_tool_never_raises_and_leaks_no_key():
    real = packs.plan_pack
    packs.plan_pack = lambda a: (_ for _ in ()).throw(RuntimeError("boom " + FAKE_KEY))
    try:
        res = mp.tool_make_modpack_file(args())
    finally:
        packs.plan_pack = real
    check("an unexpected error becomes a failure dict", res["ok"] is False and res["reason"] == "unsupported" and "unexpectedly" in res["error"], res)
    for junk in (None, "x", [], 5, {"entries": "x"}):
        res = mp.tool_make_modpack_file(junk)
        check("junk arguments %r answer a dict" % (str(junk)[:10],), isinstance(res, dict) and "ok" in res)
    os.environ["CURSEFORGE_API_KEY"] = FAKE_KEY
    try:
        out = new_dir("out")
        with World(answer=True) as w:
            res = mp.tool_make_modpack_file(args(dest=str(out)))
            blob = json.dumps(res) + json.dumps(w.asked) + Path(res["path"]).read_bytes().decode("latin-1")
        check("the key is nowhere in the result, the question or the file", FAKE_KEY not in blob)
    finally:
        os.environ.pop("CURSEFORGE_API_KEY", None)


def test_source_files_keep_the_rules():
    texts = {n: (ROOT / "jarvis-cli" / "jarvis" / n).read_text(encoding="utf-8") for n in
             ("actions/minecraft_packs.py", "modhosts/packs.py", "modhosts/outdir.py")}
    for name, text in texts.items():
        check("%s never opens a connection itself" % name, not re.search(r"urlopen|import requests|http\.client|import socket|urllib\.request|aiohttp", text))
        check("%s never touches the key or its file" % name, "x-api-key" not in text.lower() and "curseforge.json" not in text and "get_key" not in text)
    check("the tool file declares neither TOOL_PACK_INSTRUCTION nor TOOL_SETTINGS", not re.search(r"^TOOL_(PACK_INSTRUCTION|SETTINGS)\s*=", texts["actions/minecraft_packs.py"], re.M))
    check("settings are read through tool_settings.get('minecraft_lookup', ...)", 'tool_settings.get("minecraft_lookup"' in texts["modhosts/packs.py"])
    check("packs.py reuses the shared protected-places rule through outdir (path_tools._protected_reason)", "path_tools" in texts["modhosts/outdir.py"] and "_protected_reason" in texts["modhosts/outdir.py"])
    check("the tool file does not import the tool registry at module level", not re.search(r"^(from|import) .*\b(tools|ai_client|router)\b", texts["actions/minecraft_packs.py"], re.M))


# --- runner (keep BELOW every test: it reads globals() when it executes) ----

if __name__ == "__main__":
    for name in sorted(n for n in list(globals()) if n.startswith("test_")):
        print(f"\n== {name}")
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((name, f"raised {exc!r}"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for item in FAIL:
        print("  FAILED:", item)
    sys.exit(1 if FAIL else 0)
