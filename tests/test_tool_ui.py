"""TOOL_UI screens, unfinished-tool drafts, and the Tool Manager's persona/screen switches.

Covers:
  1. tool_ui.validate_ui: good entries, every way a path can try to leave the
     declaring file's directory, bad ids/modes/labels, missing files, duplicates.
  2. tool_ui.read_bundle: reads the three files, re-checks at read time, size cap.
  3. tool_loader: a module's TOOL_UI is attached to its record; a bad entry never
     rejects the file.
  4. tool_disable: persona/ui lists round trip, bad ids, forget_*, file shape.
  5. Drafts: save / list / read / delete, unchanged text, blank text skipped, history
     cap, bad keys (path traversal), prune.
  6. write_tool: a refused save is stashed as a draft; a good save with a draft key
     removes it; scaffold writes a template's folder once and never over an existing one.
  7. The two UI templates validate, ship a folder, and their tool.js / tool.html /
     tool.css exist after scaffolding.
  8. CLI: tool-ui list/bundle/run, tool-disable-set --kind, personas-list --all,
     disabled-list keys, ctools-drafts.

No network, no live model. Runs against a throwaway HOME.

Run: python3 tests/test_tool_ui.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
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
from jarvis import tool_disable, tool_loader, tool_ui  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def reset_disabled():
    for path in (tool_disable.DISABLED_FILE, tool_disable.DISABLED_FILE.with_suffix(".json.bak")):
        if path.exists():
            path.unlink()


def entry(**kw):
    base = {"id": "my_page", "label": "My page", "mode": "button", "path": "my_page"}
    base.update(kw)
    return base


work = Path(tempfile.mkdtemp(prefix="jarvis-tool-ui-"))
(work / "my_page").mkdir()
(work / "my_page" / "tool.html").write_text("<p>hi</p>", encoding="utf-8")
(work / "my_page" / "tool.js").write_text("host.toast('x')", encoding="utf-8")
(work / "my_page" / "tool.css").write_text("p{color:red}", encoding="utf-8")
(work / "deep" / "er").mkdir(parents=True)
(work / "deep" / "er" / "page.html").write_text("<b>deep</b>", encoding="utf-8")
outside = Path(tempfile.mkdtemp(prefix="jarvis-outside-"))
(outside / "tool.html").write_text("secret", encoding="utf-8")

# --- 1. validate_ui -----------------------------------------------------------------
ents, errs = tool_ui.validate_ui([entry(tool="t1")], "f.py", work, {"t1"})
check("a good entry is accepted", len(ents) == 1 and not errs, errs)
check("defaults resolve to tool.html/js/css", ents and ents[0]["files"] == {"html": "tool.html", "js": "tool.js", "css": "tool.css"}, ents)
check("the public shape carries no absolute path", ents and "_dir" not in tool_ui.public(ents[0]) and str(work) not in json.dumps(tool_ui.public(ents[0])))
ents, errs = tool_ui.validate_ui([entry(path="deep/er", html="page.html", js="", css="")], "f.py", work)
check("a nested folder and a named file work; \"\" means none", len(ents) == 1 and ents[0]["files"] == {"html": "page.html", "js": "", "css": ""}, (ents, errs))
ents, errs = tool_ui.validate_ui([entry(path="deep\\er", html="page.html")], "f.py", work)
check("backslashes in path are accepted (Windows authors)", len(ents) == 1, errs)

for label, bad in (("..", {"path": "../" + outside.name}), ("absolute", {"path": str(outside)}),
                   ("drive letter", {"path": "C:/x"}), ("empty", {"path": ""}), ("missing", {"path": None}),
                   ("dot-dot inside", {"path": "my_page/../../x"}), ("NUL", {"path": "my_page\x00"})):
    ents, errs = tool_ui.validate_ui([entry(**bad)], "f.py", work)
    check("path %s is refused" % label, not ents and errs, (ents, errs))
ents, errs = tool_ui.validate_ui([entry(html="../../" + outside.name + "/tool.html")], "f.py", work)
check("an html name that climbs out is refused", not ents and errs, (ents, errs))
ents, errs = tool_ui.validate_ui([entry(js="/etc/passwd")], "f.py", work)
check("an absolute js name is refused", not ents and errs, (ents, errs))
ents, errs = tool_ui.validate_ui([entry(js="tool.txt")], "f.py", work)
check("the wrong extension is refused", not ents and errs, (ents, errs))
try:
    link = work / "my_page" / "evil.html"
    link.symlink_to(outside / "tool.html")
    ents, errs = tool_ui.validate_ui([entry(html="evil.html")], "f.py", work)
    check("a symlink pointing out of the folder is refused", not ents and errs, (ents, errs))
except (OSError, NotImplementedError):
    print("skipped  symlink test (not permitted on this system)")

check("bad id", not tool_ui.validate_ui([entry(id="Bad Id")], "f.py", work)[0])
check("bad mode", not tool_ui.validate_ui([entry(mode="popup")], "f.py", work)[0])
check("blank label", not tool_ui.validate_ui([entry(label="  ")], "f.py", work)[0])
check("unknown tool name", not tool_ui.validate_ui([entry(tool="nope")], "f.py", work, {"t1"})[0])
ents, errs = tool_ui.validate_ui([entry(path="empty_folder")], "f.py", work)
check("a folder with nothing in it is refused with a reason", not ents and any("nothing to show" in e for e in errs), errs)
(work / "html_only").mkdir()
(work / "html_only" / "tool.html").write_text("x", encoding="utf-8")
ents, errs = tool_ui.validate_ui([entry(path="html_only", js="missing.js")], "f.py", work)
check("an explicitly named missing file is reported but the rest loads", len(ents) == 1 and any("missing" in e for e in errs) and ents[0]["missing"] == ["js"], (ents, errs))
ents, errs = tool_ui.validate_ui([entry(), entry(label="Other")], "f.py", work)
check("a duplicate id in one file keeps the first", len(ents) == 1 and any("duplicate" in e for e in errs), (ents, errs))
ents, errs = tool_ui.validate_ui("nonsense", "f.py", work)
check("a non-list TOOL_UI is an error, not a crash", not ents and errs)
ents, errs = tool_ui.validate_ui([entry()], "f.py", None)
check("no base directory drops entries with a reason", not ents and errs)
ents, errs = tool_ui.validate_ui([entry(id="u%d" % i) for i in range(20)], "f.py", work)
check("more than the cap is truncated and said so", len(ents) == tool_ui.MAX_ENTRIES_PER_FILE and errs, len(ents))

# --- 2. read_bundle -----------------------------------------------------------------
ents, _ = tool_ui.validate_ui([entry()], "f.py", work)
b = tool_ui.read_bundle(ents[0])
check("read_bundle returns the three files", b["ok"] and b["html"] == "<p>hi</p>" and b["js"] == "host.toast('x')" and b["css"] == "p{color:red}", b)
(work / "my_page" / "tool.html").write_text("x" * (tool_ui.MAX_FILE_BYTES + 10), encoding="utf-8")
b = tool_ui.read_bundle(ents[0])
check("an oversized file is skipped with a problem, others still read", b["html"] == "" and b["js"] and b["problems"], b)
(work / "my_page" / "tool.html").write_text("<p>hi</p>", encoding="utf-8")
(work / "my_page" / "tool.js").unlink()
(work / "my_page" / "tool.css").unlink()
(work / "my_page" / "tool.html").unlink()
b = tool_ui.read_bundle(ents[0])
check("files removed after discovery: ok False, no crash", b["ok"] is False and b.get("error"), b)

# --- 3. loader ---------------------------------------------------------------------
loader_dir = Path(tempfile.mkdtemp(prefix="jarvis-loader-"))
(loader_dir / "ld_page").mkdir()
(loader_dir / "ld_page" / "tool.html").write_text("<i>x</i>", encoding="utf-8")
(loader_dir / "ld_tool.py").write_text('''
def tool_ld(args):
    return {"ok": True}
TOOL_SCHEMAS = [{"name": "ld_tool", "description": "x", "parameters": {"type": "object", "properties": {}}}]
TOOLS = {"ld_tool": tool_ld}
TOOL_GROUP = "custom"
TOOL_UI = [
    {"id": "ld_page", "label": "LD", "mode": "menu", "path": "ld_page", "tool": "ld_tool"},
    {"id": "ld_bad", "label": "Bad", "mode": "menu", "path": "../nope"},
]
''', encoding="utf-8")
logs = []
recs = tool_loader.discover_actions(loader_dir, logger=logs.append)
rec = next((r for r in recs if getattr(r, "file", "") == "ld_tool.py"), None)
check("the loader keeps a module's valid TOOL_UI entries", rec is not None and rec.valid and [u["id"] for u in rec.ui] == ["ld_page"], (rec, logs))
check("a bad TOOL_UI entry is logged and the file still loads", rec is not None and rec.valid and any("[tool-ui]" in l for l in logs), logs)

# --- 4. tool_disable ---------------------------------------------------------------
reset_disabled()
check("nothing off by default", not tool_disable.disabled_personas() and not tool_disable.disabled_ui())
check("persona off round trip", tool_disable.set_persona_disabled("my-persona", True) == {"id": "my-persona", "disabled": True} and tool_disable.is_persona_disabled("my-persona"))
check("ui off round trip", tool_disable.set_ui_disabled("my_page", True)["disabled"] and tool_disable.is_ui_disabled("my_page"))
data = json.loads(tool_disable.DISABLED_FILE.read_text())
check("both live in disabled.json beside tools/commands", data.get("personas") == ["my-persona"] and data.get("ui") == ["my_page"] and "tools" in data and "commands" in data, data)
check("switching back on removes them", not tool_disable.set_ui_disabled("my_page", False)["disabled"] and not tool_disable.is_ui_disabled("my_page"))
for bad in ("", "Bad Id", "1x", "a" * 80):
    try:
        tool_disable.set_ui_disabled(bad, True)
        check("ui id %r refused" % bad, False, "no error")
    except ValueError:
        check("ui id %r refused" % bad, True)
tool_disable.forget_ui("my_page"); tool_disable.forget_persona("my-persona")
check("forget_* drop the entries and never raise", not tool_disable.disabled_ui() and not tool_disable.disabled_personas())
reset_disabled()

# --- 5. drafts ---------------------------------------------------------------------
src = "x = 1\n"
check("save_draft ok", cts.save_draft("k1", src, name="mytool", mode="new")["ok"])
check("the draft is on disk", (cts.DRAFTS_DIR / "k1.json").is_file())
check("same text again is a no-op", cts.save_draft("k1", src).get("unchanged") is True)
check("blank text is skipped, the real draft survives", cts.save_draft("k1", "   \n").get("skipped") == "empty" and cts.read_draft("k1")["source"] == src)
listed = cts.list_drafts()
check("list_drafts has it, without source", len(listed) == 1 and listed[0]["key"] == "k1" and "source" not in listed[0], listed)
for bad in ("../x", "a/b", "A", "", "x" * 80, "..\\x"):
    check("draft key %r refused" % bad, not cts.save_draft(bad, src)["ok"] and not cts.read_draft(bad)["ok"] and not cts.delete_draft(bad)["ok"])
check("no file escaped the drafts folder", not any(p.name.endswith(".json") for p in cts.TOOLS_DIR.glob("*.json") if p.name != "_meta.json"))
old_gap = cts.DRAFT_HISTORY_MIN_GAP
cts.DRAFT_HISTORY_MIN_GAP = 0
for n in range(cts.MAX_DRAFT_HISTORY + 4):
    cts.save_draft("k2", "v%d\n" % n)
d = cts.read_draft("k2")
check("history is capped", len(d["history"]) == cts.MAX_DRAFT_HISTORY and d["source"].startswith("v%d" % (cts.MAX_DRAFT_HISTORY + 3)), len(d["history"]))
cts.DRAFT_HISTORY_MIN_GAP = old_gap
check("delete_draft removes it and is idempotent", cts.delete_draft("k2")["ok"] and cts.delete_draft("k2")["ok"] and not cts.read_draft("k2")["ok"])
old = time.time() - (cts.DRAFT_MAX_AGE_DAYS + 2) * 86400
os.utime(cts.DRAFTS_DIR / "k1.json", (old, old))
cts.save_draft("k3", "y = 2\n")
check("an old draft is pruned", not (cts.DRAFTS_DIR / "k1.json").exists() and (cts.DRAFTS_DIR / "k3.json").exists())
cts.delete_draft("k3")
check("a draft is not listed as a tool file", all(t["name"] != "k3" for t in cts.list_tools()))

# --- 6. write_tool: stash, clear, scaffold ---------------------------------------------
res = cts.write_tool("broken_one", "def (:\n", draft_key="tab1")
check("a refused save says so", not res["ok"] and res.get("draft_saved") is True, res)
check("…and the text is in the draft", cts.read_draft("tab1").get("source") == "def (:\n")
check("…and nothing was written as a tool", not (cts.TOOLS_DIR / "broken_one.py").exists())
res = cts.write_tool("broken_two", "def (:\n")
check("a refused save with no key stashes under failed-<name>", res.get("draft_key", "").startswith("failed-") and cts.read_draft(res["draft_key"]).get("ok"), res)
good = cts.template_source("minimal")
res = cts.write_tool("good_one", good, draft_key="tab1")
check("a good save succeeds", res["ok"], res)
check("…and removes the draft it was made from", not cts.read_draft("tab1")["ok"])
cts.delete_draft("failed-broken_two")

# --- 7. the UI templates ------------------------------------------------------------------
ids = {t["id"]: t for t in cts.templates()}
check("both UI templates are listed with their folder", "ui_button" in ids and "ui_menu" in ids and ids["ui_button"]["folder"] == "dice_page" and ids["ui_menu"]["folder"] == "word_count_panel", ids.keys())
check("the older ui_bridge template (id ui) is still there, untouched", "ui" in ids, list(ids))
for tid, mode in (("ui_button", "button"), ("ui_menu", "menu")):
    name = "tpl_%s" % tid
    res = cts.write_tool(name, cts.template_source(tid), scaffold=tid)
    folder = ids[tid]["folder"]
    check("%s saves" % tid, res["ok"], res)
    check("%s scaffolds its folder" % tid, all((cts.TOOLS_DIR / folder / f).is_file() for f in ("tool.html", "tool.js", "tool.css")), res.get("scaffolded"))
    check("%s validates its TOOL_UI with no problems after scaffolding" % tid, res.get("ui") and res["ui"][0]["mode"] == mode and not res.get("ui_problems"), (res.get("ui"), res.get("ui_problems")))
    marker = cts.TOOLS_DIR / folder / "tool.html"
    marker.write_text("MINE", encoding="utf-8")
    res2 = cts.write_tool(name + "_b", cts.template_source(tid), scaffold=tid)
    check("%s never overwrites an existing folder" % tid, marker.read_text() == "MINE" and res2.get("scaffold_note"), res2.get("scaffold_note"))
tool_disable.set_ui_disabled("dice_page", True)
cts.delete_tool("tpl_ui_button")
check("delete_tool drops the screen's switch", not tool_disable.is_ui_disabled("dice_page"))
reset_disabled()

# --- 8. CLI -------------------------------------------------------------------------------
def cli(*args, stdin=None):
    env = dict(os.environ)
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=env, input=stdin,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


rc, out, err = cli("tool-ui", "list", "--all")
try:
    listing = json.loads(out)
except ValueError:
    listing = {}
check("tool-ui list prints JSON with a ui list", rc == 0 and isinstance(listing.get("ui"), list), (rc, out[:200], err[:200]))
rc, out, err = cli("tool-ui", "bundle", "no_such_element")
check("tool-ui bundle for an unknown id fails cleanly", rc != 0 and '"ok": false' in out.lower().replace('"ok":false', '"ok": false'), (rc, out))
rc, out, err = cli("tool-ui", "run", "no_such_element", "x")
check("tool-ui run for an unknown element is refused", rc != 0 and "no UI element" in out, (rc, out))
rc, out, err = cli("tool-disable-set", "ghost_ui", "true", "--kind", "ui")
check("switching off a UI element that doesn't exist is refused", rc != 0 and "no such UI element" in out, (rc, out))
rc, out, err = cli("tool-disable-set", "ghost-persona", "true", "--kind", "persona")
check("switching off a persona that doesn't exist is refused", rc != 0 and "no such persona" in out, (rc, out))
rc, out, err = cli("tool-disable-set", "x", "true", "--kind", "bogus")
check("an unknown --kind is refused", rc != 0, (rc, out))
rc, out, err = cli("disabled-list")
dl = json.loads(out) if rc == 0 else {}
check("disabled-list carries personas and ui", "personas" in dl and "ui" in dl, out[:200])
rc, out, err = cli("personas-list", "--all")
check("personas-list --all prints a personas list", rc == 0 and isinstance(json.loads(out).get("personas"), list), (rc, out[:200]))
rc, out, err = cli("ctools-drafts", "save", "clitab", "--stdin", "--name", "clitool", stdin="a = 1\n")
check("ctools-drafts save", rc == 0 and json.loads(out)["ok"], (rc, out, err[:200]))
rc, out, err = cli("ctools-drafts", "list")
check("ctools-drafts list shows it", rc == 0 and any(d["key"] == "clitab" for d in json.loads(out)["drafts"]), out[:300])
rc, out, err = cli("ctools-drafts", "show", "../evil")
check("ctools-drafts refuses a traversal key", rc != 0, (rc, out))
rc, out, err = cli("ctools-drafts", "delete", "clitab")
check("ctools-drafts delete", rc == 0 and json.loads(out)["ok"], (rc, out))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
