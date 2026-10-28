"""The Minecraft Menu panel and its palette commands (waves S3, M2 and M5).

Covers (static and declarative: nothing here opens a browser or the network):
  1. The panel's files exist and are wired: the Menu button, the stylesheet, the script, the click
     handler and the JarvisHost.openPanel entry.
  2. The palette registry: a `minecraft` verb in the panels group that opens that Menu item, covers
     no CLI name (the panel has no CLI subcommand, so nothing is added to reserved_names.py), and is a
     panel verb in slash-palette.js.
  3. TOOL_COMMANDS in actions/minecraft_lookup.py: /mcsearch, /mcfind, /mcproject validate against the
     real schemas, are all "safe" lookups of that file's OWN tools, do not shadow a built-in, and build
     the right arguments from typed words.
  4. The panel only names the six Minecraft tools, and that list matches the tools the action files declare.
  5. Panel rules (AGENTS.md): no native confirm/alert/prompt, no second toast helper, no HTML built from
     text, no key and no direct vendor call in the panel's JavaScript.
  6. The hand-written hand_s3_* fixtures (when present) and the M2 hand_m2_* fixtures parse.
  7. M2: the SEVENTH tool (check_mc_updates), the Updates tab, and every argument name the panel sends is one the real
     tool schema declares (checked by running the panel's argument builders under node, when node is installed).
  8. M2: /api/tools/run in web/server.js. confirm_ui is read ONLY there, ONLY as `=== true`, ONLY for the Minecraft
     tools; "y" is fed on stdin and 120000 ms allowed only on that path; every other tool still gets 30000 ms and no stdin.
  9. M2: the questions a Minecraft tool can ask while it runs are exactly the two the panel's dialog covers. A new
     ui.confirm / choose / prompt / form in these files, or an open_when_done in the download tool, fails here so the
     panel's dialog is updated BEFORE a "y" is fed to it.

  10. M5: the reworked panel (shared "what I play", cards, tray, batch). It still names only the seven tools, keeps
     nothing in the browser, reads only fields the real tools produce (checked against the action files), has its
     narrow-width, focus and reduced-motion rules, and its own hand_m5_* fixtures parse and hold no key-like text.
     The baseline zip carries no hand_s3_* / hand_m2_* fixtures, so those are only checked when present.

Run: python3 tests/test_minecraft_panel.py        (written by the S3, M2 and M5 agents; not run by them)
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

PASS, FAIL = [], []


def check(name, ok, extra=None):
    (PASS if ok else FAIL).append(name)
    if not ok:
        print("FAIL: %s%s" % (name, ("  -> %r" % (extra,)) if extra is not None else ""))


PUBLIC = ROOT / "web" / "public"
JS = PUBLIC / "minecraft-panel.js"
CSS = PUBLIC / "minecraft-panel.css"
SIX = {"search_mc_mods", "get_mc_project", "list_optifine_versions", "download_mc_file", "download_optifine",
       "make_modpack_file"}


def read(path):
    return Path(path).read_text(encoding="utf-8")


def strip_js_comments(src):
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def registry():
    text = read(PUBLIC / "slash-commands-data.js")
    m = re.search(r"/\*JSON-BEGIN\*/(.*)/\*JSON-END\*/", text, re.S)
    return json.loads(m.group(1))


# --- 1. files and wiring ---------------------------------------------------------------------
SEVEN = SIX | {"check_mc_updates"}
check("the panel script and stylesheet exist", JS.exists() and CSS.exists())
index = read(PUBLIC / "index.html")
app = read(PUBLIC / "app.js")
check("index.html has the Menu button", 'id="menu-item-minecraft"' in index)
check("index.html loads the stylesheet and the script", 'href="minecraft-panel.css"' in index and 'src="minecraft-panel.js"' in index)
check("app.js opens the panel from the Menu button", '#menu-item-minecraft' in app and "JarvisMinecraft?.open()" in app)
check("JarvisHost.openPanel knows 'minecraft'", re.search(r"minecraft:\s*\(\)\s*=>\s*window\.JarvisMinecraft\?\.open\(\)", app) is not None)

# --- 2. palette registry ---------------------------------------------------------------------
reg = registry()
verbs = {v["verb"]: v for v in reg["verbs"]}
v = verbs.get("minecraft")
check("a 'minecraft' verb is registered", v is not None)
if v:
    check("it is in the panels group and opens the Menu item", v["group"] == "panels" and v.get("menu") == "menu-item-minecraft")
    check("it is a safe instant opener with no arguments", v["riskTier"] == "safe" and v["instant"] is True and v["confirm"] is False and v["args"] == [])
    check("it covers no CLI name (the panel has no CLI subcommand)", v["covers"] == [])
check("the Menu item it names exists in index.html", 'id="%s"' % (v or {}).get("menu", "-") in index)
check("slash-palette.js lists it as a panel verb", re.search(r'PANEL_VERBS = new Set\(\[[^\]]*"minecraft"', read(PUBLIC / "slash-palette.js")) is not None)

# --- 3. TOOL_COMMANDS ------------------------------------------------------------------------
from jarvis import reserved_names, tool_commands as tc  # noqa: E402
from jarvis.actions import minecraft_lookup as lookup  # noqa: E402

schemas = {s["name"]: s for s in lookup.TOOL_SCHEMAS}
ents, errs = tc.validate_commands(lookup.TOOL_COMMANDS, "minecraft_lookup.py", lookup.TOOLS, schemas,
                                  reserved=reserved_names.RESERVED_NAMES)
check("all three commands validate with no error", len(ents) == 3 and not errs, errs)
names = {e["name"] for e in ents}
check("the commands are /mcsearch /mcfind /mcproject", names == {"mcsearch", "mcfind", "mcproject"}, names)
check("every one is a safe lookup of this file's own tools", all(e["riskTier"] == "safe" and e["tool"] in lookup.TOOLS for e in ents))
check("none of them is a built-in verb, alias or reserved CLI name",
      not (names & set(verbs)) and not (names & {a for x in reg["verbs"] for a in x.get("aliases", [])}) and not (names & set(reserved_names.RESERVED_NAMES)))
by = {e["name"]: e for e in ents}
check("/mcsearch takes the whole rest of the line as the query",
      tc.build_arguments(by["mcsearch"], ["sodium", "extra", "words"]) == ({"query": "sodium extra words"}, None))
args, err = tc.build_arguments(by["mcfind"], ["1.20.1", "fabric", "sodium", "extra"])
check("/mcfind maps version, loader, then the rest as the query", not err and args == {"mc_version": "1.20.1", "loader": "fabric", "query": "sodium extra"}, (args, err))
args, err = tc.build_arguments(by["mcproject"], ["sodium"])
check("/mcproject works with just a project", not err and args == {"project": "sodium"}, (args, err))
args, err = tc.build_arguments(by["mcproject"], ["sodium", "1.20.1", "quilt"])
check("/mcproject takes an optional version and loader", not err and args == {"project": "sodium", "mc_version": "1.20.1", "loader": "quilt"}, (args, err))
args, err = tc.build_arguments(by["mcfind"], ["1.20.1"])
check("/mcfind without its query is refused with a message", err, (args, err))
check("only the lookup file names TOOL_COMMANDS (downloads and packs are never typed commands)",
      all("TOOL_COMMANDS" not in read(ROOT / "jarvis-cli" / "jarvis" / "actions" / f)
          for f in ("minecraft_download.py", "minecraft_optifine.py", "minecraft_packs.py")))

# --- 4. the six tools ------------------------------------------------------------------------
from jarvis.actions import minecraft_download, minecraft_optifine, minecraft_packs, minecraft_updates  # noqa: E402

declared = set()
for mod in (lookup, minecraft_download, minecraft_optifine, minecraft_packs, minecraft_updates):
    declared |= {s_["name"] for s_ in mod.TOOL_SCHEMAS}
check("the five action files declare exactly the seven tools", declared == SEVEN, declared)
js = read(JS)
m = re.search(r"const TOOLS = \[([^\]]*)\]", js)
panel_tools = set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()
check("the panel's own list matches them", panel_tools == SEVEN, panel_tools)
used = set(re.findall(r'runTool\("([a-z_]+)"', js)) | set(re.findall(r'call\("([a-z_]+)"', js))
check("every tool the panel runs is one of the seven", used and used <= SEVEN, used)
check("the panel has an Updates tab that runs check_mc_updates", '["updates", "Updates"]' in js and 'runTool("check_mc_updates"' in js)
check("check_mc_updates is a read: it asks for no confirmation of its own", "check_mc_updates" not in minecraft_updates.TOOL_CONFIRM_REQUIRED)

# --- 5. panel rules --------------------------------------------------------------------------
code = strip_js_comments(js)
check("no native confirm / alert / prompt", re.search(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\(", code) is None)
check("no second toast helper or toast host", not re.search(r"^\s*(?:async\s+)?function\s+toast\b|\bconst\s+toast\s*=|jui-toasts|ntf-stack|new\s+Notification", code, re.M))
check("no HTML is built from text", not any(w in code for w in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc")))
check("the panel never names the key file or a key reader", re.search(r"curseforge\.json|get_key|api[_-]?key|x-api-key", code, re.I) is None)
check("the panel only calls the tool routes", set(re.findall(r'"(/api/[^"]*)"', code)) == {"/api/tools/preview", "/api/tools/run"}, set(re.findall(r'"(/api/[^"]*)"', code)))
check("the panel makes no request to a vendor", re.search(r"fetch\(\s*[\"']https?:", code) is None)
check("vendor links are made only through safeUrl", "target: \"_blank\"" in code and code.count("target:") == 1 and "safeUrl(url)" in code)
check("the panel keeps the preview-then-run gate", code.index('"/api/tools/preview"') < code.index('"/api/tools/run"'))
check("a real write is always confirmed (sure: true on queue, the tray's batch, OptiFine and pack: dry run + real pack = 2)", code.count("sure: true") == 5)
check("no word 'install' in any text the panel shows, except as \"not installed\" (nothing is ever installed)",
      not [t for t in re.findall(r'"(?:[^"\\\n]|\\.)*"', code) if re.search(r"install", re.sub(r"not installed", "", t, flags=re.I), re.I)])
css = read(CSS)
check("the stylesheet uses only :root tokens for colour", re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css) is None)
check("the stylesheet is scoped to the panel", all(sel.strip().startswith((".mc-", ".menu-panel--minecraft", "@media", "@keyframes mc-", "}")) or not sel.strip() or sel.strip().startswith(("*", "/*", " *"))
      for sel in re.findall(r"^([^\s{][^{]*)\{", css, re.M)))

# --- 6. fixtures -----------------------------------------------------------------------------
DATA = ROOT / "tests" / "data" / "modhosts"
# tests/data is git-ignored, so the S3 fixtures only exist where the owner added them with `git add -f`: whatever is there must parse.
s3 = sorted(p.name for p in DATA.glob("hand_s3_*.json"))
for name in s3:
    try:
        ok = isinstance(json.loads(read(DATA / name)), dict)
    except Exception:  # noqa: BLE001
        ok = False
    check("%s parses" % name, ok)
# The baseline zip carries no hand_m2_* either: check whichever are present (M2 asked for all seven with `git add -f`).
m2 = sorted(p.name for p in DATA.glob("hand_m2_*.json"))
for name in m2:
    try:
        ok = isinstance(json.loads(read(DATA / name)), dict)
    except Exception:  # noqa: BLE001
        ok = False
    check("%s parses" % name, ok)
M5 = ["hand_m5_dry_download.json", "hand_m5_hostile.json", "hand_m5_pack_cross_dry.json", "hand_m5_pack_cross_needs_download.json",
      "hand_m5_pack_cross_needs_download_nosize.json", "hand_m5_pack_cross_written.json", "hand_m5_project.json",
      "hand_m5_project_nofit.json", "hand_m5_project_unfiltered.json", "hand_m5_search_both_cf.json",
      "hand_m5_search_both_partial.json", "hand_m5_search_empty.json", "hand_m5_search_modrinth.json", "hand_m5_updates.json",
      "hand_m5_updates_ask.json", "hand_m5_updates_hostile.json", "hand_m5_updates_needs_folder.json"]
m5 = sorted(p.name for p in DATA.glob("hand_m5_*.json"))
check("the hand_m5_ fixtures are all there (git add -f: tests/data is git-ignored)", m5 == M5, sorted(set(M5) ^ set(m5)))
for name in m5:
    try:
        ok = isinstance(json.loads(read(DATA / name)), dict)
    except Exception:  # noqa: BLE001
        ok = False
    check("%s parses" % name, ok)
check("no fixture holds anything that looks like a key", not any(re.search(r"[A-Za-z0-9_\-]{32,}", read(DATA / n)) for n in s3 + m2 + m5))

# --- 7. the panel only sends argument names the real schemas declare ----------------------------
import shutil  # noqa: E402
import subprocess  # noqa: E402

SCHEMAS = {}
for mod in (lookup, minecraft_download, minecraft_optifine, minecraft_packs, minecraft_updates):
    for sc in mod.TOOL_SCHEMAS:
        SCHEMAS[sc["name"]] = sc["parameters"]["properties"]
node = shutil.which("node")
if not node:
    print("NOTE: node is not installed, so the argument names the panel sends were not compared with the tool schemas")
else:
    script = r"""
const fs = require('fs'), vm = require('vm');
const sb = { URL }; vm.createContext(sb);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);
const P = sb.JarvisMinecraft._pure;
const dl = { project: 'a', source: 'modrinth', kind: 'mod', channel: 'beta', mc_version: '1.20.1', loader: 'fabric', file: 'f', dest: 'D:/x', filename: 'x.jar', with_dependencies: true };
const out = {
  search_mc_mods: P.searchArgs({ query: 'q', kind: 'mod', sort: 'downloads', source: 'both', mc_version: '1.20.1', loader: 'fabric', limit: 5, offset: 5 }),
  get_mc_project: P.projectArgs({ project: 'p', source: 'modrinth', channel: 'beta', mc_version: '1.20.1', loader: 'fabric', files_limit: 5 }),
  download_mc_file: P.downloadArgs(dl, true),
  list_optifine_versions: P.optifineListArgs({ mc_version: '1.20.1', include_preview: true }),
  download_optifine: P.optifineArgs({ mc_version: '1.20.1', version: 'I6', preview: true, dest: 'D:/x', filename: 'o.jar' }, true),
  make_modpack_file: P.packArgs({ format: 'curseforge', entries_from: 'modrinth', download_to_match: true, name: 'n', version: '1', author: 'a', summary: 's', mc_version: '1.20.1',
    loader: 'fabric', loader_version: '0.15', entries: 'a | f | beta', channel: 'beta', src: 'D:/s', dest: 'D:/d', filename: 'f', include_dependencies: false, overwrite: true }, false),
  check_mc_updates: P.updatesArgs({ folder: 'D:/m', mc_version: '1.20.1', loader: 'fabric', channel: 'beta' }),
};
process.stdout.write(JSON.stringify(out));
"""
    done = subprocess.run([node, "-e", script, str(JS)], capture_output=True, text=True, timeout=60)
    try:
        built = json.loads(done.stdout)
    except Exception:  # noqa: BLE001
        built = None
    check("the panel's argument builders ran under node", isinstance(built, dict), done.stderr[:300])
    if isinstance(built, dict):
        for tool, args in sorted(built.items()):
            check("%s: every argument name the panel sends is in the tool's schema" % tool, set(args) <= set(SCHEMAS[tool]), sorted(set(args) - set(SCHEMAS[tool])))
        entry_keys = set(SCHEMAS["make_modpack_file"]["entries"]["items"]["properties"])
        for e in built["make_modpack_file"]["entries"]:
            check("a pack entry only has keys the schema declares", set(e) <= entry_keys, sorted(set(e) - entry_keys))
        enums = {"channel": ("make_modpack_file", "check_mc_updates", "download_mc_file"), "loader": ("check_mc_updates",)}
        for key, tools_ in enums.items():
            for t in tools_:
                allowed = SCHEMAS[t][key].get("enum")
                if allowed and key in built[t]:
                    check("%s.%s: the value the panel sends is one the schema allows" % (t, key), built[t][key] in allowed, built[t][key])
        check("make_modpack_file: entries_from and download_to_match are real arguments", {"entries_from", "download_to_match"} <= set(SCHEMAS["make_modpack_file"]))

# --- 8. /api/tools/run: the one place confirm_ui means anything --------------------------------
server = read(ROOT / "web" / "server.js")
start = server.index('app.post("/api/tools/run"')
end = server.index('app.post("/api/tools/preview"')
handler = server[start:end]
check("confirm_ui appears only inside the /api/tools/run handler", server.count("confirm_ui") == handler.count("confirm_ui") and handler.count("confirm_ui") >= 1)
check("it is read as a strict boolean", "req.body?.confirm_ui === true" in handler)
tools_m = re.search(r"const MINECRAFT_TOOLS = \[([^\]]*)\]", handler)
listed = set(re.findall(r'"([a-z_]+)"', tools_m.group(1))) if tools_m else set()
check("only the Minecraft group's seven tools can use it", listed == SEVEN, listed)
check("the tool name is checked against that list", "MINECRAFT_TOOLS.includes(name)" in handler)
check("the yes is fed on stdin as a fixed number of lines", '"y\\n".repeat(64)' in handler)
check("a confirm_ui run is allowed 120000 ms", "120000" in handler)
check("any other call is exactly what it was: 30000 ms, only JARVIS_UI, no stdin", 'runJarvisOnce(["tool-run", name, argsJson, mode], 30000, { JARVIS_UI: "web" })' in handler)
check("the preview handler never reads confirm_ui", "confirm_ui" not in server[end:end + 4000].split("app.post(\"/api/tools/safety\"")[0])
check("runJarvisOnce already takes stdin (no new parameter was needed)", re.search(r"function runJarvisOnce\(args, timeoutMs = 10000, extraEnv = \{\}, stdinText = null\)", server) is not None)

# --- 9. the questions a Minecraft tool can ask while it runs ----------------------------------
PKG = ROOT / "jarvis-cli" / "jarvis"
files = sorted((PKG / "actions").glob("minecraft_*.py")) + sorted((PKG / "modhosts").glob("*.py"))
titles, calls, others = set(), 0, []
for f_ in files:
    text_ = read(f_)
    calls += len(re.findall(r"\bui\.confirm\(", text_))
    titles |= set(re.findall(r'\bui\.confirm\(\s*"([^"]+)"', text_))
    others += [f_.name for w in ("choose", "prompt", "form") if re.search(r"\bui\.%s\(" % w, text_)]
check("the only questions are 'Save outside the downloads library?' and 'Download mod files to match them on CurseForge?'",
      titles == {"Save outside the downloads library?", "Download mod files to match them on CurseForge?"}, titles)
check("every ui.confirm in them has a literal title (so the line above sees it)", calls == len(re.findall(r'\bui\.confirm\(\s*"', "".join(read(f_) for f_ in files))), calls)
check("none of them asks a choose / prompt / form question", not others, others)
check("the download tool never asks the hub to open a file when it is done (a yes would turn that on)", "open_when_done" not in read(PKG / "actions" / "minecraft_download.py"))
check("the download tool queues one link per call (the hub's bulk question needs more than its limit)", read(PKG / "actions" / "minecraft_download.py").count("[plan[\"url\"]]") >= 2)

# --- 10. M5: the reworked panel -----------------------------------------------------------------
check("nothing is kept in the browser (no localStorage, sessionStorage, IndexedDB or cookie)", re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie", code) is None)
check("the four tabs are Find, Updates, OptiFine and Make a pack, and the tray is a side region",
      '[["find", "Find"], ["updates", "Updates"], ["optifine", "OptiFine"], ["pack", "Make a pack"]]' in js and 'el("aside", { class: "mc-tray"' in js)
check("the tab list is a real tab list (roles, arrow keys) and the panel a labelled dialog",
      all(w in js for w in ('role: "tablist"', 'role: "tab"', 'setAttribute("role", "tabpanel")', '"ArrowRight"', '"aria-modal": "true"', '"aria-controls"')))
check("results are announced politely and loading is marked busy", 'aria-live' in js and '"aria-busy"' in js)
check("every control is labelled (fields are wrapped in a label)", 'el("label", { class: "mc-field" }' in js and "mc-sr" in js)
check("a dry run of a writer opens no dialog of its own, and a batch's real calls say their dialog was shown", "dryRunNeedsNoDialog" in js and "dialogShown: true" in js)
check("the stylesheet has a visible focus rule, a narrow-width layout, stacked table rows and reduced motion",
      ":focus-visible" in css and "@media (max-width: 820px)" in css and "mc-table--stack" in css and "prefers-reduced-motion" in css)
check("the stylesheet has light and dark through tokens only (no colour literal; colour-mix of tokens is fine)", "var(--green)" in css and "var(--red)" in css and re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css) is None)
check("the layout keeps its scroll inside the body and the tray (the top never moves)", "scrollbar-gutter: stable" in css and ".mc-work{" in css)

# The fields the cards, the project view and the tabs read must be ones the real tools produce.
sample = {"source": "modrinth", "id": "AANobbMI", "slug": "sodium", "name": "Sodium", "summary": "s", "author": "a", "downloads": 5,
          "updated": "2026-01-01T00:00:00Z", "kind": "mod", "mc_versions": ["1.20.1"], "loaders": ["fabric"], "page_url": "https://modrinth.com/mod/sodium"}
hit_keys = set(lookup._shape_hit(sample))
check("a search hit carries every field the cards draw", {"source", "id", "slug", "name", "summary", "author", "downloads", "updated", "kind",
                                                          "mc_versions", "loaders", "page_url"} <= hit_keys, hit_keys)
cf_hit = lookup._shape_hit(dict(sample, source="curseforge", distributable=False))
check("a CurseForge hit says when its author blocks downloads (the card shows that)", cf_hit.get("distributable") is False, cf_hit)
fsample = {"file_id": "u1", "file_name": "x.jar", "version_label": "1", "mc_versions": ["1.20.1"], "loaders": ["fabric"], "channel": "release",
           "date": "2026-01-01", "size": 5, "downloadable": True, "via": "fabric",
           "dependencies": [{"project_id": "P7dR8mSB", "type": "required"}]}
fshape = lookup._shape_file(fsample, {"P7dR8mSB": "Fabric API"})
check("a file carries every field the files table draws, and its required dependencies with names",
      {"file_id", "name", "version", "mc_versions", "loaders", "channel", "date", "size", "downloadable", "via", "requires"} <= set(fshape)
      and fshape["requires"] == [{"project_id": "P7dR8mSB", "name": "Fabric API"}], fshape)
lookup_src = read(ROOT / "jarvis-cli" / "jarvis" / "actions" / "minecraft_lookup.py")
check("get_mc_project answers with the recommended file, the total and the notes the project view reads",
      all(('"%s"' % k) in lookup_src for k in ("recommended_file_id", "files_total", "unfiltered", "note", "via_note", "loader_note",
                                              "distribution_note")) and "alternatives=" in lookup_src and "candidates=" in lookup_src)
check("search answers with the per-site lists and errors the cards and the partial notices read",
      all(w in lookup_src for w in ('"%s_hits"', '"%s_total"', '"%s_error"', '"%s_reason"')))
upd_src = read(ROOT / "jarvis-cli" / "jarvis" / "modhosts" / "updates.py")
check("check_mc_updates really answers needs_folder with suggested_folder and ask (the Updates tab asks for the folder)",
      all(w in upd_src for w in ('"needs_folder": True', '"suggested_folder"', 'out["ask"]')))
dl_src = read(ROOT / "jarvis-cli" / "jarvis" / "actions" / "minecraft_download.py")
check("a download's dry run carries the message, the folder and the dependencies the dialog shows",
      all(w in dl_src for w in ('out["message"]', '"dest": landing', "dependencies_would_queue", "dry_run")))
check("the dry-run message says nothing was downloaded or installed (the dialog adds 'downloaded, not installed')", "nothing was downloaded or installed" in dl_src)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
