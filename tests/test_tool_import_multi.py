"""Import choices for a tool with its folders: replace what exists, several tools at once,
and screen text backed up with a draft.

Three gaps from the tool-folders plan entry:
  1. OVERWRITE   -- `overwrite=True` lets an existing tool and existing screen files be
     replaced, each keeping a .bak; a link or a folder in the way is still a blocker; a failed
     import puts the replaced files back exactly as they were.
  2. SEVERAL TOOLS -- a bundle with more than one top-level .py imports each as its own tool
     (names from the files, or a `names` map); a duplicate name, or two tools declaring the
     same tool / screen / command, refuses the whole bundle; one refused tool writes nothing.
  3. SCREEN DRAFTS -- a draft carries the tab's unsaved screen text; a save that leaves `web`
     out keeps it, an empty dict clears it, bad paths / non-text / oversize are refused, and
     it is only ever a copy (nothing appears under the tools folder).
  4. The plan shows the script of a .js / .html screen file (for the read-first review).
  5. CLI: ctools-import --names/--overwrite, ctools-drafts save --json.

No network, no live model. Runs against a throwaway HOME. NOT RUN by the author of the
patch -- run it:

    python3 tests/test_tool_import_multi.py
"""
import json
import os
import subprocess
import sys
import tempfile
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

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def source(tool, ui_id=None, folder=None, command=None):
    ui = ""
    if ui_id:
        ui = "TOOL_UI = [{\"id\": \"%s\", \"label\": \"L\", \"mode\": \"button\", \"path\": \"%s\"}]\n" % (ui_id, folder)
    return (
        "def tool_%(t)s(args):\n    return {\"ok\": True}\n\n"
        "TOOL_SCHEMAS = [{\"name\": \"%(t)s\", \"description\": \"d\", "
        "\"parameters\": {\"type\": \"object\", \"properties\": {}, \"required\": []}}]\n"
        "TOOLS = {\"%(t)s\": tool_%(t)s}\nTOOL_GROUP = \"custom\"\n" % {"t": tool} + ui
    )


def cli(*args, stdin=None):
    env = dict(os.environ, PYTHONPATH=str(CLI_DIR))
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], input=stdin, capture_output=True,
                       text=True, env=env, cwd=str(CLI_DIR))
    return p.returncode, p.stdout, p.stderr


cts.ensure_dir()
TOOLS = cts.TOOLS_DIR

# --- 1. overwrite --------------------------------------------------------------------
PY_A = source("mo_tool_a", "mo_ui_a", "mo_page_a")
bundle = [("mo_a.py", PY_A), ("mo_page_a/tool.html", "<p>new</p>"), ("mo_page_a/tool.js", "/* new */")]
r = cts.import_bundle(bundle, name="mo_a", write=True)
check("first import works", r.get("ok"), r)
check("... and reports nothing replaced", r.get("replaced") == {"tools": [], "files": []}, r.get("replaced"))
(TOOLS / "mo_page_a" / "tool.html").write_text("<p>mine</p>", encoding="utf-8")

plan = cts.import_bundle(bundle, name="mo_a")
check("plan without overwrite: the tool and the file are blockers, and listed as collisions",
      plan["name_problem"].endswith("already exists") and any("already exists" in p for p in plan["problems"])
      and plan["collisions"]["tools"] == ["mo_a"] and "mo_page_a/tool.html" in plan["collisions"]["files"], plan)
check("... exists flags are set on the rows", any(w["exists"] for w in plan["web"]) and plan["tools"][0]["exists"], plan)
plan = cts.import_bundle(bundle, name="mo_a", overwrite=True)
check("plan with overwrite: no blockers, the same things are listed as replaced",
      plan["name_problem"] == "" and plan["problems"] == [] and plan["replaces"]["tools"] == ["mo_a"]
      and "mo_page_a/tool.html" in plan["replaces"]["files"], plan)
r = cts.import_bundle(bundle, name="mo_a", write=True)
check("write without overwrite is still refused and changes nothing",
      not r.get("ok") and (TOOLS / "mo_page_a" / "tool.html").read_text(encoding="utf-8") == "<p>mine</p>", r)

old_py = (TOOLS / "mo_a.py").read_text(encoding="utf-8")
r = cts.import_bundle(bundle, name="mo_a", write=True, overwrite=True)
check("write with overwrite succeeds", r.get("ok"), r)
check("... the screen file is replaced and its old text kept in a .bak",
      (TOOLS / "mo_page_a" / "tool.html").read_text(encoding="utf-8") == "<p>new</p>"
      and (TOOLS / "mo_page_a" / "tool.html.bak").read_text(encoding="utf-8") == "<p>mine</p>", r)
check("... the tool is replaced with a .bak of the old one",
      (TOOLS / "mo_a.py.bak").exists() and (TOOLS / "mo_a.py.bak").read_text(encoding="utf-8") == old_py)
check("... and the result says what was replaced",
      r["replaced"]["tools"] == ["mo_a"] and "mo_page_a/tool.html" in r["replaced"]["files"], r.get("replaced"))

# a link in the way is never written through, overwrite or not
outside = Path(tempfile.mkdtemp(prefix="jarvis-outside-")) / "secret.html"
outside.write_text("keep me", encoding="utf-8")
(TOOLS / "mo_page_l").mkdir()
try:
    (TOOLS / "mo_page_l" / "tool.html").symlink_to(outside)
    linked = True
except (OSError, NotImplementedError):
    linked = False
if linked:
    lb = [("mo_l.py", source("mo_tool_l", "mo_ui_l", "mo_page_l")), ("mo_page_l/tool.html", "x")]
    plan = cts.import_bundle(lb, overwrite=True)
    check("a link in the way stays a blocker even with overwrite", any("link" in p for p in plan["problems"]), plan)
    r = cts.import_bundle(lb, write=True, overwrite=True)
    check("... and what it points at is untouched", not r.get("ok") and outside.read_text(encoding="utf-8") == "keep me", r)

# a refused tool puts replaced files back as they were
(TOOLS / "mo_page_a" / "tool.html").write_text("<p>precious</p>", encoding="utf-8")
(TOOLS / "mo_page_a" / "tool.html.bak").write_text("<p>older</p>", encoding="utf-8")
before_py = (TOOLS / "mo_a.py").read_text(encoding="utf-8")
bad = [("mo_a.py", source("click", "mo_ui_a", "mo_page_a")), ("mo_page_a/tool.html", "<p>from a bad import</p>")]
r = cts.import_bundle(bad, name="mo_a", write=True, overwrite=True)
check("a bundle whose tool is refused (built-in clash) fails", not r.get("ok"), r)
check("... the replaced screen file and its .bak are back as they were",
      (TOOLS / "mo_page_a" / "tool.html").read_text(encoding="utf-8") == "<p>precious</p>"
      and (TOOLS / "mo_page_a" / "tool.html.bak").read_text(encoding="utf-8") == "<p>older</p>")
check("... and the existing tool is exactly as it was", (TOOLS / "mo_a.py").read_text(encoding="utf-8") == before_py)

# --- 2. several tools ----------------------------------------------------------------
multi = [("mo_b.py", source("mo_tool_b", "mo_ui_b", "mo_page_b")), ("mo_c.py", source("mo_tool_c", "mo_ui_c", "mo_page_c")),
         ("mo_page_b/tool.html", "<b/>"), ("mo_page_c/tool.html", "<c/>"), ("mo_page_c/tool.js", "/*c*/"),
         ("mo_page_zz/tool.html", "unused")]
plan = cts.import_bundle(multi)
check("plan: two tools, named from their files, screen folders shared",
      plan.get("ok") and [t["name"] for t in plan["tools"]] == ["mo_b", "mo_c"] and len(plan["web"]) == 4, plan)
check("plan: the top-level fields describe the first tool", plan["name"] == "mo_b" and plan["py_file"] == "mo_b.py", plan)
plan = cts.import_bundle(multi, names={"mo_b.py": "mo_two", "mo_c.py": "mo_two"})
check("plan: two tools given the same name is a name problem", "both be saved as" in plan["name_problem"], plan)
check("plan: a single `name` is ignored when there are several tools",
      [t["name"] for t in cts.import_bundle(multi, name="same")["tools"]] == ["mo_b", "mo_c"])
many = [("t%02d.py" % i, source("mo_many_%d" % i)) for i in range(13)]
check("plan: more than 12 tools is refused", "at most 12" in cts.import_bundle(many).get("error", ""))

r = cts.import_bundle(multi, names={"mo_b.py": "mo_b_new"}, write=True)
check("write: both tools import", r.get("ok") and r["names"] == ["mo_b_new", "mo_c"], r)
check("... each .py is on disk", (TOOLS / "mo_b_new.py").exists() and (TOOLS / "mo_c.py").exists())
check("... the screen files either tool uses are written, the unused folder is not",
      (TOOLS / "mo_page_b" / "tool.html").exists() and (TOOLS / "mo_page_c" / "tool.js").exists() and not (TOOLS / "mo_page_zz").exists())
check("... `tools` lists both tools' functions", set(r["tools"]) == {"mo_tool_b", "mo_tool_c"}, r.get("tools"))

dup = [("mo_d.py", source("mo_tool_dup", "mo_ui_d", "mo_page_d")), ("mo_e.py", source("mo_tool_dup", "mo_ui_e", "mo_page_e")),
       ("mo_page_d/tool.html", "d"), ("mo_page_e/tool.html", "e")]
r = cts.import_bundle(dup, write=True)
check("two tools declaring the same tool function refuse the whole bundle, nothing written",
      not r.get("ok") and "mo_tool_dup" in r.get("error", "") and not (TOOLS / "mo_d.py").exists() and not (TOOLS / "mo_page_d").exists(), r)
dup_ui = [("mo_f.py", source("mo_tool_f", "mo_ui_same", "mo_page_f")), ("mo_g.py", source("mo_tool_g", "mo_ui_same", "mo_page_g")),
          ("mo_page_f/tool.html", "f"), ("mo_page_g/tool.html", "g")]
r = cts.import_bundle(dup_ui, write=True)
check("two tools declaring the same screen id refuse the whole bundle",
      not r.get("ok") and "mo_ui_same" in r.get("error", "") and not (TOOLS / "mo_f.py").exists(), r)

half = [("mo_h.py", source("mo_tool_h")), ("mo_i.py", "def broken(:\n")]
r = cts.import_bundle(half, write=True)
check("one refused tool means none are written", not r.get("ok") and not (TOOLS / "mo_h.py").exists() and not (TOOLS / "mo_i.py").exists(), r)
check("... and the answer names the file that was refused", r.get("py_file") == "mo_i.py", r)

# --- 3. screen text in drafts --------------------------------------------------------
SRC = source("mo_draft_tool")
r = cts.save_draft("d_mo1", SRC, name="mo_draft_tool", mode="edit", orig_name="mo_draft_tool",
                   web={"mo_page_a/tool.html": "<p>unsaved</p>", "mo_page_a/tool.js": "// wip"})
check("a draft saves with screen text", r.get("ok"), r)
d = cts.read_draft("d_mo1")
check("... and gives it back", d.get("web") == {"mo_page_a/tool.html": "<p>unsaved</p>", "mo_page_a/tool.js": "// wip"}, d)
summary = [x for x in cts.list_drafts() if x["key"] == "d_mo1"]
check("... the list says how many screen files it holds",
      summary and sorted(summary[0]["web_files"]) == ["mo_page_a/tool.html", "mo_page_a/tool.js"], summary)
check("... it is a copy: nothing was written under the tools folder", not (TOOLS / "mo_draft_tool.py").exists())
r = cts.save_draft("d_mo1", SRC + "\n# edited", name="mo_draft_tool", mode="edit", orig_name="mo_draft_tool")
check("a save that leaves `web` out keeps the screen text",
      r.get("ok") and cts.read_draft("d_mo1")["web"].get("mo_page_a/tool.js") == "// wip", r)
r = cts.save_draft("d_mo1", SRC + "\n# edited", name="mo_draft_tool", mode="edit", orig_name="mo_draft_tool",
                   web={"mo_page_a/tool.js": "// wip"})
check("the same source and screen text again is 'unchanged'", r.get("unchanged"), r)
r = cts.save_draft("d_mo1", SRC + "\n# edited", name="mo_draft_tool", mode="edit", orig_name="mo_draft_tool", web={})
check("an empty `web` clears it", r.get("ok") and cts.read_draft("d_mo1")["web"] == {}, r)
for label, bad_web in (("a traversal path", {"../x.js": "1"}), ("a path that isn't a screen file", {"a/b.txt": "1"}),
                       ("non-text", {"a/b.js": 5}), ("a NUL byte", {"a/b.js": "x\x00y"}),
                       ("over the size limit", {"a/b.js": "x" * (300 * 1024)}), ("a list instead of an object", ["a"])):
    r = cts.save_draft("d_mo2", SRC, name="mo_draft_tool", mode="edit", orig_name="mo_draft_tool", web=bad_web)
    check("a draft with %s is refused" % label, r.get("ok") is False, r)
check("... and nothing was stored for it", cts.read_draft("d_mo2").get("ok") is False)

# --- 4. the plan shows scripts ---------------------------------------------------------
plan = cts.import_bundle([("mo_s.py", source("mo_tool_s", "mo_ui_s", "mo_page_s")), ("mo_page_s/tool.js", "a\nb\nc"),
                          ("mo_page_s/tool.html", "<script>x()</script>"), ("mo_page_s/tool.css", "p{}")])
rows = {w["path"]: w for w in plan["web"]}
check("plan: .js and .html rows carry their text and line count, .css does not",
      rows["mo_page_s/tool.js"]["head"] == "a\nb\nc" and rows["mo_page_s/tool.js"]["lines"] == 3
      and "<script>" in rows["mo_page_s/tool.html"]["head"] and "head" not in rows["mo_page_s/tool.css"], rows)
long_js = "\n".join("line %d" % i for i in range(500))
plan = cts.import_bundle([("mo_s.py", source("mo_tool_s")), ("mo_page_s/tool.js", long_js)])
row = plan["web"][0]
check("plan: a long script is cut to the first lines but its real line count is kept",
      row["lines"] == 500 and row["head"].count("\n") + 1 == cts.IMPORT_READ_LINES, row)

# --- 5. CLI ----------------------------------------------------------------------------
stdin = json.dumps({"files": [{"path": "cli_a.py", "text": source("mo_cli_a")}, {"path": "cli_b.py", "text": source("mo_cli_b")}]})
rc, out, err = cli("ctools-import", "--stdin", "--names", json.dumps({"cli_a.py": "mo_cli_first"}), stdin=stdin)
plan = json.loads(out) if out.strip() else {}
check("ctools-import --names renames one tool of several in the plan",
      rc == 0 and [t["name"] for t in plan.get("tools", [])] == ["mo_cli_first", "cli_b"], (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-import", "--stdin", "--names", "not json", stdin=stdin)
check("ctools-import --names with bad JSON fails with a sentence", rc != 0, (rc, out[:200], err[:200]))
rc, out, err = cli("ctools-import", "--stdin", "--write", "--names", json.dumps({"cli_a.py": "mo_cli_first"}), stdin=stdin)
check("ctools-import --write imports both tools",
      rc == 0 and (TOOLS / "mo_cli_first.py").exists() and (TOOLS / "cli_b.py").exists(), (rc, out[:300], err[:200]))
rc, out, err = cli("ctools-import", "--stdin", "--write", "--names", json.dumps({"cli_a.py": "mo_cli_first"}), stdin=stdin)
check("... a second --write is refused without --overwrite", rc != 0 or not json.loads(out).get("ok"), (rc, out[:200]))
rc, out, err = cli("ctools-import", "--stdin", "--write", "--overwrite", "--names", json.dumps({"cli_a.py": "mo_cli_first"}), stdin=stdin)
check("... and goes through with --overwrite", rc == 0 and json.loads(out).get("ok"), (rc, out[:300], err[:200]))
body = json.dumps({"source": source("mo_cli_draft"), "web": {"x/tool.js": "// from the cli"}})
rc, out, err = cli("ctools-drafts", "save", "d_mo_cli", "--stdin", "--json", "--name", "mo_cli_draft", "--mode", "edit", "--orig", "mo_cli_draft", stdin=body)
check("ctools-drafts save --json stores the screen text",
      rc == 0 and cts.read_draft("d_mo_cli")["web"] == {"x/tool.js": "// from the cli"}, (rc, out[:200], err[:200]))
rc, out, err = cli("ctools-drafts", "save", "d_mo_cli", "--stdin", "--json", stdin="[1]")
check("... and --json with something that isn't an object fails", rc != 0, (rc, out[:200]))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
