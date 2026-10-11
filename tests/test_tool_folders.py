"""A tool's screen files: edit them, import a tool with its folders, uninstall with them.

The three L.46.3 gaps this closes -- no multi-file editor tabs, no folder-tool import,
no uninstall that removes the folder -- all rest on one rule (custom_tools_store.py,
"WEB FILES"): a tool may only touch the files its own saved TOOL_UI resolves to.

Covers:
  1. tool_ui: `planned` names on every element; `element_files` lists existing and
     not-yet-existing files, drops anything that escapes, skips a kind set to "".
  2. web_footprint / read_tool("...")["web"] / list_tools()["web_folders"].
  3. read_web_file / write_web_file: only the tool's own screen files, a missing one is
     created, a .bak is kept, traversal / unlisted / oversize / NUL refused, a symlink
     out of the folder refused.
  4. Uninstall: default keeps folders; remove_folders removes served files and the
     emptied folder; other files are kept and the folder with them; a folder another tool
     names (quoted path) or uses is left alone; "_" folders and links are never touched;
     a mere substring ("ui" inside other words) does NOT block removal.
  5. Import: plan (one wrapping folder stripped, junk and helper modules skipped, two or
     no .py refused, unsafe path refuses the whole bundle, name taken / file would be
     overwritten reported); write (files a screen uses are written, unused ones skipped,
     never over an existing file, rolled back when the .py is refused, check runs against
     the staged folder); the same from a .zip (zip-slip refused, too many entries refused).
  6. CLI: ctools-files list/read/write/removal, ctools-import --stdin / --zip, and
     ctools-delete --folders.

No network, no live model. Runs against a throwaway HOME. NOT RUN by the author of the
patch -- run it:

    python3 tests/test_tool_folders.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

from jarvis import custom_tools_store as cts  # noqa: E402
from jarvis import tool_ui  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def source(tool="fold_tool_a", ui_id="fold_ui_a", folder="fold_page_a", extra=""):
    """A tool file with one screen in `folder`."""
    return (
        "def tool_%(tool)s(args):\n"
        "    return {\"ok\": True}\n\n"
        "TOOL_SCHEMAS = [{\"name\": \"%(tool)s\", \"description\": \"d\", "
        "\"parameters\": {\"type\": \"object\", \"properties\": {}, \"required\": []}}]\n"
        "TOOLS = {\"%(tool)s\": tool_%(tool)s}\n"
        "TOOL_GROUP = \"custom\"\n"
        "TOOL_UI = [{\"id\": \"%(ui)s\", \"label\": \"Fold\", \"mode\": \"button\", \"path\": \"%(folder)s\"%(extra)s}]\n"
        % {"tool": tool, "ui": ui_id, "folder": folder, "extra": extra}
    )


def put_folder(folder, files=("tool.html", "tool.js", "tool.css")):
    root = cts.TOOLS_DIR / folder
    root.mkdir(parents=True, exist_ok=True)
    for name in files:
        (root / name).write_text("/* %s */" % name, encoding="utf-8")
    return root


cts.ensure_dir()
TOOLS = cts.TOOLS_DIR

# --- 1. tool_ui: planned names and element_files ------------------------------------
work = Path(tempfile.mkdtemp(prefix="jarvis-folders-"))
(work / "pg").mkdir()
(work / "pg" / "tool.html").write_text("<p/>", encoding="utf-8")
(work / "pg" / "tool.js").write_text("1", encoding="utf-8")
ents, errs = tool_ui.validate_ui([{"id": "pg_ui", "label": "P", "mode": "button", "path": "pg"}], "f.py", work, set())
check("a valid element carries `planned` names for all three kinds",
      len(ents) == 1 and ents[0]["planned"] == {"html": "tool.html", "js": "tool.js", "css": "tool.css"}, (ents, errs))
files = tool_ui.element_files(tool_ui.public(ents[0]), work)
check("element_files lists the css that doesn't exist yet as exists=False",
      [(f["kind"], f["rel"], f["exists"]) for f in files] ==
      [("html", "pg/tool.html", True), ("js", "pg/tool.js", True), ("css", "pg/tool.css", False)], files)
ents, _ = tool_ui.validate_ui([{"id": "pg_ui", "label": "P", "mode": "button", "path": "pg", "css": ""}], "f.py", work, set())
check("a kind set to \"\" is not planned and not listed",
      "css" not in ents[0]["planned"] and [f["kind"] for f in tool_ui.element_files(tool_ui.public(ents[0]), work)] == ["html", "js"], ents)
forged = dict(tool_ui.public(ents[0]), planned={"html": "../../x.html"})
check("element_files drops a planned name that escapes the folder",
      all(".." not in f["rel"] for f in tool_ui.element_files(forged, work)), tool_ui.element_files(forged, work))

# --- 2. footprint -------------------------------------------------------------------
put_folder("fold_page_a", ("tool.html", "tool.js"))
res = cts.write_tool("fold_tool_a", source())
check("a tool declaring a screen saves", res.get("ok"), res)
shown = cts.read_tool("fold_tool_a")
web = shown.get("web") or {}
check("read_tool carries web.folders", web.get("folders") == ["fold_page_a"], web)
check("read_tool carries web.files with the missing css marked new",
      sorted((f["rel"], f["exists"]) for f in web.get("files", [])) ==
      [("fold_page_a/tool.css", False), ("fold_page_a/tool.html", True), ("fold_page_a/tool.js", True)], web)
listed = [t for t in cts.list_tools() if t["name"] == "fold_tool_a"]
check("list_tools carries web_folders", listed and listed[0].get("web_folders") == ["fold_page_a"], listed)

# --- 3. read / write one screen file ------------------------------------------------
r = cts.read_web_file("fold_tool_a", "fold_page_a/tool.js")
check("read_web_file reads a screen file", r.get("ok") and r["text"] == "/* tool.js */" and r["exists"], r)
r = cts.read_web_file("fold_tool_a", "fold_page_a/tool.css")
check("a screen file that doesn't exist reads as empty, exists=False", r.get("ok") and r["text"] == "" and r["exists"] is False, r)
r = cts.write_web_file("fold_tool_a", "fold_page_a/tool.js", "host.toast('hi');\n")
check("write_web_file saves it", r.get("ok") and not r["created"], r)
check("... and keeps one .bak of what it replaced",
      (TOOLS / "fold_page_a" / "tool.js.bak").read_text(encoding="utf-8") == "/* tool.js */")
check("... and the new text is on disk", (TOOLS / "fold_page_a" / "tool.js").read_text(encoding="utf-8") == "host.toast('hi');\n")
r = cts.write_web_file("fold_tool_a", "fold_page_a/tool.css", "p{color:red}")
check("writing the missing css creates it", r.get("ok") and r["created"] and (TOOLS / "fold_page_a" / "tool.css").exists(), r)
for bad, why in (("../_meta.json", "traversal"), ("fold_tool_a.py", "the tool's own .py is not a screen file"),
                 ("fold_page_a/other.js", "an unlisted file"), ("/etc/passwd", "absolute"), ("C:/x.js", "drive letter"),
                 ("", "empty")):
    r = cts.write_web_file("fold_tool_a", bad, "x")
    check("write_web_file refuses %s" % why, not r.get("ok"), r)
    r = cts.read_web_file("fold_tool_a", bad)
    check("read_web_file refuses %s" % why, not r.get("ok"), r)
check("another tool's name can't be used to reach this tool's file",
      not cts.read_web_file("no_such_tool", "fold_page_a/tool.js").get("ok"))
r = cts.write_web_file("fold_tool_a", "fold_page_a/tool.js", "x" * (tool_ui.MAX_FILE_BYTES + 1))
check("an oversize screen file is refused", not r.get("ok"), r)
r = cts.write_web_file("fold_tool_a", "fold_page_a/tool.js", "a\x00b")
check("a NUL is refused", not r.get("ok"), r)
secret = Path(tempfile.mkdtemp(prefix="jarvis-secret-")) / "tool.html"
secret.write_text("secret", encoding="utf-8")
link_dir = TOOLS / "fold_page_link"
try:
    os.symlink(str(secret.parent), str(link_dir))
    linked = True
except (OSError, NotImplementedError):
    linked = False
if linked:
    res = cts.write_tool("fold_tool_link", source("fold_tool_link", "fold_ui_link", "fold_page_link"))
    r = cts.read_web_file("fold_tool_link", "fold_page_link/tool.html")
    check("a folder that is a link out of the tools folder can't be read through", not r.get("ok") or r.get("text") != "secret", r)
    r = cts.write_web_file("fold_tool_link", "fold_page_link/tool.html", "overwritten")
    check("... or written through", secret.read_text(encoding="utf-8") == "secret", r)
    cts.delete_tool("fold_tool_link")
    os.unlink(str(link_dir))

# --- 4. uninstall --------------------------------------------------------------------
put_folder("fold_page_b")
cts.write_tool("fold_tool_b", source("fold_tool_b", "fold_ui_b", "fold_page_b"))
res = cts.delete_tool("fold_tool_b")
check("the default delete leaves the folder, as before", res.get("ok") and (TOOLS / "fold_page_b" / "tool.html").exists(), res)
put_folder("fold_page_c")
(TOOLS / "fold_page_c" / "tool.js.bak").write_text("old", encoding="utf-8")
cts.write_tool("fold_tool_c", source("fold_tool_c", "fold_ui_c", "fold_page_c"))
preview = cts.removal_preview("fold_tool_c")
fold = (preview.get("folders") or [{}])[0]
check("removal_preview lists the served files and the .bak, blocks nothing",
      fold.get("path") == "fold_page_c" and not fold.get("blocked")
      and sorted(fold.get("remove", [])) == ["fold_page_c/tool.css", "fold_page_c/tool.html", "fold_page_c/tool.js", "fold_page_c/tool.js.bak"]
      and fold.get("keep") == [], preview)
res = cts.delete_tool("fold_tool_c", remove_folders=True)
check("remove_folders removes the screen files and the emptied folder",
      res.get("ok") and "fold_page_c" in res.get("folders_removed", []) and not (TOOLS / "fold_page_c").exists(), res)
check("... and the tool file is gone", not cts._resolve("fold_tool_c")[0])

put_folder("fold_page_d")
(TOOLS / "fold_page_d" / "notes.txt").write_text("keep me", encoding="utf-8")
(TOOLS / "fold_page_d" / "sub").mkdir()
(TOOLS / "fold_page_d" / "sub" / "extra.js").write_text("1", encoding="utf-8")
cts.write_tool("fold_tool_d", source("fold_tool_d", "fold_ui_d", "fold_page_d"))
res = cts.delete_tool("fold_tool_d", remove_folders=True)
check("anything that isn't a served file is kept, and so is the folder",
      (TOOLS / "fold_page_d" / "notes.txt").exists() and not (TOOLS / "fold_page_d" / "tool.html").exists()
      and any(k["path"] == "fold_page_d" for k in res.get("folders_kept", [])), res)
check("a served file in a subfolder goes; the emptied subfolder goes", not (TOOLS / "fold_page_d" / "sub").exists(), res)

put_folder("fold_page_e")
cts.write_tool("fold_tool_e", source("fold_tool_e", "fold_ui_e", "fold_page_e"))
other = source("fold_tool_f", "fold_ui_f", "fold_page_f") + "# shares \"fold_page_e/\" with fold_tool_e\nSHARED = \"fold_page_e/tool.js\"\n"
(TOOLS / "fold_tool_f.py.disabled").write_text(other, encoding="utf-8")        # disabled: not loaded, still protects
res = cts.delete_tool("fold_tool_e", remove_folders=True)
check("a folder another tool file names (even a disabled one) is left alone",
      (TOOLS / "fold_page_e" / "tool.html").exists() and any(k["path"] == "fold_page_e" for k in res.get("folders_kept", [])), res)
(TOOLS / "fold_tool_f.py.disabled").unlink()

put_folder("ui")
cts.write_tool("fold_tool_g", source("fold_tool_g", "fold_ui_g", "ui"))
(TOOLS / "fold_tool_h.py").write_text(source("fold_tool_h", "fold_ui_h", "fold_page_h") + "# builds a quick UI with a guide\n", encoding="utf-8")
res = cts.delete_tool("fold_tool_g", remove_folders=True)
check("the letters 'ui' inside other words do NOT protect a folder called ui",
      "ui" in res.get("folders_removed", []) and not (TOOLS / "ui").exists(), res)
(TOOLS / "fold_tool_h.py").unlink()

(TOOLS / "_drafts").mkdir(exist_ok=True)
(TOOLS / "_drafts" / "keep.json").write_text("{}", encoding="utf-8")
(TOOLS / "_drafts" / "tool.html").write_text("<p/>", encoding="utf-8")      # a screen file, or the TOOL_UI entry is dropped as "nothing to show"
cts.write_tool("fold_tool_i", source("fold_tool_i", "fold_ui_i", "_drafts"))
res = cts.delete_tool("fold_tool_i", remove_folders=True)
check("a folder starting with _ is never touched", (TOOLS / "_drafts" / "keep.json").exists()
      and (TOOLS / "_drafts" / "tool.html").exists()
      and any(k["path"] == "_drafts" for k in res.get("folders_kept", [])), res)

# --- 5. import -----------------------------------------------------------------------
GOOD_PY = source("imp_tool_a", "imp_ui_a", "imp_page_a")
bundle = [("imp_tool_a/imp_tool_a.py", GOOD_PY), ("imp_tool_a/imp_page_a/tool.html", "<p>x</p>"),
          ("imp_tool_a/imp_page_a/tool.js", "1"), ("imp_tool_a/imp_page_a/notes.txt", "n"),
          ("imp_tool_a/.DS_Store", "junk"), ("imp_tool_a/__pycache__/x.pyc", "junk"),
          ("imp_tool_a/_helper.py", "x = 1"), ("imp_tool_a/README.md", "# hi")]
plan = cts.import_bundle(bundle)
check("plan: the wrapping folder is stripped and the name comes from the .py",
      plan.get("ok") and plan["py_file"] == "imp_tool_a.py" and plan["name"] == "imp_tool_a", plan)
check("plan: only html/js/css screen files are listed",
      [w["path"] for w in plan["web"]] == ["imp_page_a/tool.html", "imp_page_a/tool.js"], plan)
why = {s["path"]: s["why"] for s in plan["skipped"]}
check("plan: other kinds and helper modules are skipped with a reason, junk silently",
      "imp_page_a/notes.txt" in why and "README.md" in why and "_helper.py" in why and ".DS_Store" not in why, why)
check("plan: nothing was written and nothing ran", not (TOOLS / "imp_tool_a.py").exists() and not (TOOLS / "imp_page_a").exists())
check("plan: no blockers", plan["problems"] == [] and plan["name_problem"] == "", plan)
check("plan: the first lines of the .py are returned for the review", "def tool_imp_tool_a" in plan.get("py_head", ""), plan)

check("plan: no .py is refused", not cts.import_bundle([("a/tool.html", "x")]).get("ok"))
check("plan: two .py files are refused",
      "found 2" in cts.import_bundle([("a.py", GOOD_PY), ("b.py", GOOD_PY)]).get("error", ""))
check("plan: a .. path refuses the whole bundle",
      "unsafe path" in cts.import_bundle([("a.py", GOOD_PY), ("../evil.js", "x")]).get("error", ""))
check("plan: an absolute path refuses the whole bundle", not cts.import_bundle([("/etc/x.py", GOOD_PY)]).get("ok"))
check("plan: a drive-lettered path refuses the whole bundle", not cts.import_bundle([("C:/x.py", GOOD_PY)]).get("ok"))
check("plan: flat files (no wrapping folder) keep their layout",
      cts.import_bundle([("t.py", GOOD_PY), ("pg/tool.html", "x")])["web"][0]["path"] == "pg/tool.html")
check("plan: Windows separators are accepted",
      cts.import_bundle([("t\\t.py", GOOD_PY), ("t\\pg\\tool.html", "x")])["web"][0]["path"] == "pg/tool.html")
check("plan: a name that is already taken is a name problem, not a file problem",
      cts.import_bundle(bundle, name="fold_tool_a")["name_problem"].endswith("already exists"))
check("plan: a bad name is reported", cts.import_bundle(bundle, name="Bad Name")["name_problem"] != "")
put_folder("imp_page_clash", ("tool.html",))
clash = cts.import_bundle([("c.py", source("imp_tool_c", "imp_ui_c", "imp_page_clash")), ("imp_page_clash/tool.html", "x")])
check("plan: a screen file that would overwrite an existing one is a blocker",
      any("already exists" in p for p in clash["problems"]), clash)
r = cts.import_bundle([("c.py", source("imp_tool_c", "imp_ui_c", "imp_page_clash")), ("imp_page_clash/tool.html", "x")], write=True)
check("write: refused while there is a blocker, nothing written",
      not r.get("ok") and not (TOOLS / "c.py").exists() and not cts._resolve("c")[0], r)
check("... and the existing file is untouched", (TOOLS / "imp_page_clash" / "tool.html").read_text(encoding="utf-8") == "/* tool.html */")

r = cts.import_bundle(bundle, write=True)
check("write: imports the tool", r.get("ok") and r.get("tools") == ["imp_tool_a"], r)
check("write: the .py and the used screen files are on disk",
      (TOOLS / "imp_tool_a.py").exists() and (TOOLS / "imp_page_a" / "tool.html").exists() and (TOOLS / "imp_page_a" / "tool.js").exists())
check("write: the screen resolves against its real folder", [u["id"] for u in r.get("ui", [])] == ["imp_ui_a"], r.get("ui"))
check("write: a file no declared screen uses is not written",
      not (TOOLS / "imp_page_a" / "notes.txt").exists())
check("write: the result says what was imported and what was ignored",
      r["imported"][0] == "imp_tool_a.py" and any(s["path"] == "imp_page_a/notes.txt" for s in r["skipped"]), r)
check("write: a second import of the same bundle is refused (name taken)", not cts.import_bundle(bundle, write=True).get("ok"))

# a .py that is refused leaves no screen files behind
BAD_PY = "def broken(:\n"
r = cts.import_bundle([("bad.py", BAD_PY), ("imp_page_bad/tool.html", "x")], write=True)
check("write: a .py with a syntax error is refused", not r.get("ok") and r.get("stage") == "syntax", r)
check("... and no screen files or folders were left behind", not (TOOLS / "imp_page_bad").exists())
# a .py that validates against the staging folder but is refused by the real save (builtin clash)
CLASH_PY = source("click", "imp_ui_click", "imp_page_click")
r = cts.import_bundle([("click_tool.py", CLASH_PY), ("imp_page_click/tool.html", "x")], write=True)
check("write: a .py that clashes with a built-in tool is refused and leaves nothing behind",
      not r.get("ok") and not (TOOLS / "imp_page_click").exists(), r)
# unused files only
r = cts.import_bundle([("plain.py", source("imp_tool_p", "imp_ui_p", "imp_page_p")), ("elsewhere/tool.html", "x")], write=True)
check("write: a screen file in a folder the tool doesn't declare is skipped (the tool still imports)",
      r.get("ok") and not (TOOLS / "elsewhere").exists() and any(s["path"] == "elsewhere/tool.html" for s in r["skipped"]), r)
check("... and a tool with no screen files at all imports as a plain .py",
      cts.import_bundle([("plain2.py", source("imp_tool_q", "imp_ui_q", "imp_page_q").split("TOOL_UI")[0])], write=True).get("ok"))
check("write: bytes with a BOM and CRLF are accepted from a zip-style source",
      cts.import_bundle([("bom.py", ("\ufeff" + source("imp_tool_r", "imp_ui_r", "imp_page_r").split("TOOL_UI")[0]).encode("utf-8"))], write=True).get("ok"))
check("plan: a binary file is skipped, not imported",
      any(s["why"].startswith("not a") for s in cts.import_bundle([("t.py", GOOD_PY), ("pg/tool.js", b"\xff\xfe\x00\x00")])["skipped"]))
check("plan: name_from_filename matches the browser's rule",
      cts.name_from_filename("My Tool-v2.py") == "my_tool_v2" and cts.name_from_filename("9lives.py") == "tool_9lives"
      and cts.name_from_filename("a/b/Thing.py.disabled") == "thing" and cts.name_from_filename("---.py") == "")

# zip
def make_zip(members, path):
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


zdir = Path(tempfile.mkdtemp(prefix="jarvis-zips-"))
zp = make_zip([("zip_tool/zip_tool.py", source("zip_tool", "zip_ui", "zip_page")), ("zip_tool/zip_page/tool.html", "<b>z</b>"),
               ("zip_tool/zip_page/tool.js", "2")], zdir / "ok.zip")
items = cts.read_zip_bundle(zp)
check("read_zip_bundle returns the members", sorted(n for n, _ in items) ==
      ["zip_tool/zip_page/tool.html", "zip_tool/zip_page/tool.js", "zip_tool/zip_tool.py"], items)
r = cts.import_bundle(items, write=True)
check("a zip imports like a folder", r.get("ok") and (TOOLS / "zip_page" / "tool.js").exists(), r)
evil = make_zip([("z.py", GOOD_PY), ("../../escape.js", "x")], zdir / "evil.zip")
r = cts.import_bundle(cts.read_zip_bundle(evil), write=True)
check("a zip-slip member refuses the whole zip, writes nothing", not r.get("ok") and "unsafe path" in r.get("error", "")
      and not (TOOLS.parent.parent / "escape.js").exists(), r)
(zdir / "junk.zip").write_bytes(b"not a zip")
try:
    cts.read_zip_bundle(zdir / "junk.zip")
    check("a file that isn't a zip is refused with a sentence", False, "no error")
except ValueError as exc:
    check("a file that isn't a zip is refused with a sentence", "zip" in str(exc), exc)
many = make_zip([("f%d.js" % i, "x") for i in range(cts.IMPORT_MAX_ENTRIES + 1)], zdir / "many.zip")
try:
    cts.read_zip_bundle(many)
    check("a zip with too many files is refused", False, "no error")
except ValueError as exc:
    check("a zip with too many files is refused", "at most" in str(exc), exc)

# --- 6. CLI ---------------------------------------------------------------------------
def cli(*args, stdin=None):
    env = dict(os.environ)
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=env, input=stdin,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


put_folder("cli_page")
cts.write_tool("cli_tool", source("cli_tool", "cli_ui", "cli_page"))
rc, out, err = cli("ctools-files", "list", "cli_tool")
listing = json.loads(out) if out.strip() else {}
check("ctools-files list", rc == 0 and listing.get("web", {}).get("folders") == ["cli_page"], (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-files", "read", "cli_tool", "cli_page/tool.js")
check("ctools-files read", rc == 0 and json.loads(out)["text"] == "/* tool.js */", (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-files", "write", "cli_tool", "cli_page/tool.js", "--stdin", stdin="alert(1)\n")
check("ctools-files write (text on stdin)", rc == 0 and (TOOLS / "cli_page" / "tool.js").read_text(encoding="utf-8") == "alert(1)\n", (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-files", "write", "cli_tool", "../_meta.json", "--stdin", stdin="x")
check("ctools-files write refuses a path that isn't the tool's own", rc != 0, (rc, out[:300]))
rc, out, err = cli("ctools-files", "removal", "cli_tool")
check("ctools-files removal previews", rc == 0 and json.loads(out)["folders"][0]["path"] == "cli_page", (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-files")
check("ctools-files with no arguments prints usage", rc != 0 and "usage" in out, (rc, out))

payload = json.dumps({"files": [{"path": "cim/cim.py", "text": source("cli_imp", "cli_imp_ui", "cim_page")},
                                {"path": "cim/cim_page/tool.html", "text": "<i>c</i>"}]})
rc, out, err = cli("ctools-import", "--stdin", stdin=payload)
pv = json.loads(out) if out.strip() else {}
check("ctools-import --stdin previews without writing", rc == 0 and pv.get("name") == "cim" and not (TOOLS / "cim.py").exists(), (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-import", "--stdin", "--write", "--name", "cli_imp", stdin=payload)
check("ctools-import --stdin --write imports under the chosen name",
      rc == 0 and (TOOLS / "cli_imp.py").exists() and (TOOLS / "cim_page" / "tool.html").exists(), (rc, out[:400], err[:200]))
rc, out, err = cli("ctools-import", "--stdin", stdin="not json")
check("ctools-import with bad stdin fails with a sentence", rc != 0 and json.loads(out).get("error"), (rc, out))
zp2 = make_zip([("cz/cz.py", source("cli_zip", "cli_zip_ui", "cz_page")), ("cz/cz_page/tool.css", "b{}")], zdir / "cli.zip")
rc, out, err = cli("ctools-import", "--zip", str(zp2), "--write")
check("ctools-import --zip --write", rc == 0 and (TOOLS / "cz.py").exists() and (TOOLS / "cz_page" / "tool.css").exists(), (rc, out[:400], err[:200]))
rc, out, err = cli("ctools-delete", "cli_tool", "--folders")
check("ctools-delete --folders removes the folder", rc == 0 and not (TOOLS / "cli_page").exists() and not (TOOLS / "cli_tool.py").exists(), (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-delete", "cz")
check("ctools-delete without --folders leaves it", rc == 0 and (TOOLS / "cz_page" / "tool.css").exists(), (rc, out[:300], err[:200]))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
