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

  11. M3: the reworked Updates tab (watched folders, check and update, undo). The panel now names ELEVEN tools: the six it
     already used plus check_mc_watched, apply_mc_update, mc_watch, list_mc_replaced and undo_mc_update (the one-off
     check_mc_updates run is gone from the tab; the tool itself is untouched). The tool-list checks compare only with the
     action files that exist in the tree: minecraft_update_source.py (patch M1) and minecraft_undo.py (patch M2) are
     imported when they are there. What can only hold once those two patches are applied is in test_needs_m1_* and
     test_needs_m2_* below; everything else passes with only the M3 patch applied.

  12. Round 10 (MC-A): the folder picker the panel stands aside for is read FROM path-picker.js (so the two cannot drift
     apart again), a pack can be built from a tray with items of both platforms (every entry sends its own source; the
     tool's schema allows it), and the Pack and OptiFine results scroll into view. The hand_10a_* fixtures parse.

Run: python3 tests/test_minecraft_panel.py        (written by the S3, M2, M5, M3 and MC-A agents; not run by them)
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
# M3: the five tools of the Updates tab. M1 declares the first three (actions/minecraft_update_source.py), M2 the last two
# (actions/minecraft_undo.py); both files come from other patches, so nothing below needs them to exist except test_needs_*.
M1_TOOLS = {"check_mc_watched", "apply_mc_update", "mc_watch"}
M2_TOOLS = {"list_mc_replaced", "undo_mc_update"}
NEW_FIVE = M1_TOOLS | M2_TOOLS
ELEVEN = SIX | NEW_FIVE


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

# --- 4. the tools the panel names -------------------------------------------------------------
from jarvis.actions import minecraft_download, minecraft_optifine, minecraft_packs, minecraft_updates  # noqa: E402

declared = set()
for mod in (lookup, minecraft_download, minecraft_optifine, minecraft_packs, minecraft_updates):
    declared |= {s_["name"] for s_ in mod.TOOL_SCHEMAS}
check("the five action files declare exactly the seven tools", declared == SEVEN, declared)

# M3: the two action files of the Updates tab come from the M1 and M2 patches. Import them when they are there; compare
# only with what imports (a missing file is not a failure of THIS patch: test_needs_m1_* / test_needs_m2_* say so).
import importlib  # noqa: E402


def optional_module(name):
    try:
        return importlib.import_module("jarvis.actions." + name)
    except ImportError:
        return None


update_source = optional_module("minecraft_update_source")
undo_mod = optional_module("minecraft_undo")
declared_all = set(declared)
absent = set()
for mod_, tools_ in ((update_source, M1_TOOLS), (undo_mod, M2_TOOLS)):
    if mod_ is None:
        absent |= tools_
    else:
        declared_all |= {s_["name"] for s_ in mod_.TOOL_SCHEMAS}
js = read(JS)
m = re.search(r"const TOOLS = \[([^\]]*)\]", js)
panel_tools = set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()
check("the panel's own list is the six tools it always had plus the five of the Updates tab", panel_tools == ELEVEN, panel_tools)
check("every tool the action files declare is in the panel's list, except the one-off check_mc_updates the tab no longer runs",
      (declared_all - {"check_mc_updates"}) <= panel_tools, sorted((declared_all - {"check_mc_updates"}) - panel_tools))
check("every tool in the panel's list is declared by an action file that exists (or belongs to a patch that is not applied yet)",
      panel_tools <= (declared_all | absent), sorted(panel_tools - (declared_all | absent)))
used = set(re.findall(r'runTool\("([a-z_]+)"', js)) | set(re.findall(r'call\("([a-z_]+)"', js))
check("every tool the panel runs is one of the eleven", used and used <= ELEVEN, used)
check("the Updates tab runs check_mc_watched, mc_watch (list), list_mc_replaced and, through its flows, apply_mc_update, mc_watch and undo_mc_update",
      '[["find", "Find"], ["updates", "Updates"]' in js.replace("\n", "") and 'runTool("check_mc_watched"' in js and 'runTool("mc_watch"' in js
      and 'runTool("list_mc_replaced"' in js and 'call("apply_mc_update"' in js and 'call("undo_mc_update"' in js and 'call("mc_watch"' in js)
check("the one-off check_mc_updates is not run by the panel any more, and it stays a read in its own file",
      'runTool("check_mc_updates"' not in js and "check_mc_updates" not in minecraft_updates.TOOL_CONFIRM_REQUIRED)

# What needs the M1 and M2 patches (they come before this one in the owner's order, so these hold once all are applied).
def test_needs_m1_update_source_tools():
    check("M1 (needs patch M1): actions/minecraft_update_source.py imports and declares exactly check_mc_watched, apply_mc_update and mc_watch",
          update_source is not None and {s_["name"] for s_ in update_source.TOOL_SCHEMAS} == M1_TOOLS,
          None if update_source is None else {s_["name"] for s_ in update_source.TOOL_SCHEMAS})
    if update_source is None:
        return
    confirm = set(getattr(update_source, "TOOL_CONFIRM_REQUIRED", ()))
    check("M1 (needs patch M1): apply_mc_update is confirm-gated by name and check_mc_watched is a read", "apply_mc_update" in confirm and "check_mc_watched" not in confirm, confirm)
    props = {s_["name"]: s_["parameters"]["properties"] for s_ in update_source.TOOL_SCHEMAS}
    check("M1 (needs patch M1): check_mc_watched takes no arguments the panel would have to send", not props.get("check_mc_watched"), props.get("check_mc_watched"))
    check("M1 (needs patch M1): apply_mc_update takes id and dry_run", {"id", "dry_run"} <= set(props.get("apply_mc_update", {})), props.get("apply_mc_update"))
    check("M1 (needs patch M1): mc_watch takes every argument the panel sends",
          {"action", "folder", "label", "mc_version", "loader", "channel", "auto", "dry_run"} <= set(props.get("mc_watch", {})), props.get("mc_watch"))
    actions = props.get("mc_watch", {}).get("action", {}).get("enum")
    check("M1 (needs patch M1): mc_watch's action accepts list, add, remove and set (the four the panel sends)", not actions or {"list", "add", "remove", "set"} <= set(actions), actions)


def test_needs_m2_undo_tools():
    check("M2 (needs patch M2): actions/minecraft_undo.py imports and declares exactly list_mc_replaced and undo_mc_update",
          undo_mod is not None and {s_["name"] for s_ in undo_mod.TOOL_SCHEMAS} == M2_TOOLS,
          None if undo_mod is None else {s_["name"] for s_ in undo_mod.TOOL_SCHEMAS})
    if undo_mod is None:
        return
    confirm = set(getattr(undo_mod, "TOOL_CONFIRM_REQUIRED", ()))
    check("M2 (needs patch M2): undo_mc_update is confirm-gated by name and list_mc_replaced is a read", "undo_mc_update" in confirm and "list_mc_replaced" not in confirm, confirm)
    props = {s_["name"]: s_["parameters"]["properties"] for s_ in undo_mod.TOOL_SCHEMAS}
    check("M2 (needs patch M2): undo_mc_update takes id and dry_run", {"id", "dry_run"} <= set(props.get("undo_mc_update", {})), props.get("undo_mc_update"))
    check("M2 (needs patch M2): list_mc_replaced takes nothing the panel would have to send", not props.get("list_mc_replaced"), props.get("list_mc_replaced"))


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
check("a real write is always confirmed (sure: true on queue, the tray's batch, OptiFine, pack: dry run + real pack = 2, and M3's watched-folder change, update and undo = 3)", code.count("sure: true") == 8)
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
# M3: the M1 and M2 files count too when they are in the tree, so the panel's argument names are compared with their real schemas.
for mod in [m_ for m_ in (lookup, minecraft_download, minecraft_optifine, minecraft_packs, minecraft_updates, update_source, undo_mod) if m_ is not None]:
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
  make_modpack_file_mixed: P.packArgs({ format: 'modrinth', name: 'n', mc_version: '1.20.1', loader: 'fabric',
    entries: P.entriesText([P.trayItemFrom({ source: 'modrinth', id: 'M1', name: 'a' }, { file_id: 'f1', version: '1' }), P.trayItemFrom({ source: 'curseforge', id: '394468', name: 'b' }, null)], 'typed | | beta', { sources: true, own: 'modrinth' }) }, true),
  mc_watch_list: P.watchArgs('list', {}, false),
  mc_watch_add: P.watchArgs('add', { folder: 'D:/m', label: 'L', mc_version: '1.20.1', loader: 'fabric', channel: 'beta', auto: true }, true),
  mc_watch_set: P.watchArgs('set', { folder: 'D:/m', label: 'L', mc_version: '1.20.1', loader: 'fabric', channel: 'beta', auto: false }, false),
  mc_watch_remove: P.watchArgs('remove', { folder: 'D:/m' }, true),
  apply_mc_update: P.applyArgs('mod-id-1', true),
  undo_mc_update: P.applyArgs('rep-id-1', false),
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
            real = "mc_watch" if tool.startswith("mc_watch") else ("make_modpack_file" if tool.startswith("make_modpack_file") else tool)
            if real not in SCHEMAS:
                continue                    # the M1 / M2 tools: compared in test_needs_m1_* / test_needs_m2_* once their files exist
            check("%s: every argument name the panel sends is in the tool's schema" % tool, set(args) <= set(SCHEMAS[real]), sorted(set(args) - set(SCHEMAS[real])))
        entry_keys = set(SCHEMAS["make_modpack_file"]["entries"]["items"]["properties"])
        for e in built["make_modpack_file"]["entries"]:
            check("a pack entry only has keys the schema declares", set(e) <= entry_keys, sorted(set(e) - entry_keys))
        # Round 10: a mixed pack sends `source` on EVERY entry, and the schema allows exactly those values.
        source_enum = SCHEMAS["make_modpack_file"]["entries"]["items"]["properties"].get("source", {}).get("enum")
        mixed_built = built["make_modpack_file_mixed"]
        check("a mixed pack: every entry sends a source, a value the schema allows (modrinth or curseforge)",
              bool(source_enum) and all(e.get("source") in source_enum for e in mixed_built["entries"]) and len(mixed_built["entries"]) == 3, mixed_built["entries"])
        check("a mixed pack: each entry only has keys the schema declares", all(set(e) <= entry_keys for e in mixed_built["entries"]))
        check("a mixed pack: entries_from is the OTHER platform than the pack's format, and is an allowed value",
              mixed_built.get("entries_from") == "curseforge" and mixed_built["entries_from"] in SCHEMAS["make_modpack_file"]["entries_from"].get("enum", []) and mixed_built["format"] == "modrinth", mixed_built)
        check("a mixed pack: each entry sends the platform it was added from (the tray's own), a typed line the pack's",
              [e["source"] for e in mixed_built["entries"]] == ["modrinth", "curseforge", "modrinth"], mixed_built["entries"])
        enums = {"channel": ("make_modpack_file", "download_mc_file"), "loader": ()}
        for key, tools_ in enums.items():
            for t in tools_:
                allowed = SCHEMAS[t][key].get("enum")
                if allowed and key in built[t]:
                    check("%s.%s: the value the panel sends is one the schema allows" % (t, key), built[t][key] in allowed, built[t][key])
        # M3: what the watched-folder builder sends is only ever an argument name from the brief and the value sets the panel knows.
        for name_, args_ in sorted(built.items()):
            if name_.startswith("mc_watch"):
                check("%s: only the argument names the M3 brief lists" % name_, set(args_) <= {"action", "folder", "label", "mc_version", "loader", "channel", "auto", "dry_run"}, sorted(args_))
        check("apply_mc_update / undo_mc_update: only id and dry_run", set(built["apply_mc_update"]) <= {"id", "dry_run"} and set(built["undo_mc_update"]) <= {"id", "dry_run"})
        check("a real call carries no dry_run, a dry run does, the read carries neither",
              "dry_run" not in built["mc_watch_set"] and built["mc_watch_add"].get("dry_run") is True and built["apply_mc_update"].get("dry_run") is True
              and "dry_run" not in built["undo_mc_update"] and set(built["mc_watch_list"]) == {"action"})
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
check("only the Minecraft group's tools can use it: the seven it had plus the five of the Updates tab", listed == SEVEN | NEW_FIVE, listed)
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

# --- 11. M3: the Updates tab ---------------------------------------------------------------------
check("the Updates tab says plainly what it is: runs on this PC, old file in a backup folder, the CurseForge route unverified",
      "Updates run on this PC; Jarvis keeps the old file in a backup folder; the CurseForge route is unverified." in js)
check("the Updates tab says the folder's own tick is not enough: the hub's master switch in Menu > Updates has to be on too",
      "Jarvis updates it by itself only when the master switch in Menu > Updates is also on" in js)
check("the Updates tab has the three parts, in order: watched folders, check and update, undo",
      0 < js.index('"Watched folders"') < js.index('"Check and update"') < js.index('"Undo"'))
check("a folder is added with the path picker the other tabs use", "pickFolder(form, \"folder\")" in js and "global.JarvisPathPicker" in js)
check("a watched-folder change, an update and an undo each make a dry run first and then ONE dialog (their flows are in the panel)",
      all(("async function %s(" % f_) in js for f_ in ("watchSteps", "applySteps", "undoSteps")) and "WATCH_DIALOG" in js)
check("the stylesheet has the Updates tab's classes", all(w in css for w in (".mc-section", ".mc-watchform", ".mc-lines", ".mc-results--tight")))

# --- 12. Round 10 (MC-A) --------------------------------------------------------------------------
# 12a. The shared folder picker. The panel's Tab trap and Escape stand aside while the picker is open; to know that, it
# has to name what the picker REALLY adds to the page. It once named `.pp-overlay, .path-picker`, which the picker never
# had. Everything below is read from path-picker.js, so a rename there fails here.
picker_js = read(PUBLIC / "path-picker.js")
picker_css = read(PUBLIC / "path-picker.css")
m_back = re.search(r'const backdrop = el\("div", \{ cls: "([^"]+)" \}, \[card\]\);', picker_js)
m_card = re.search(r'const card = el\("div", \{ cls: "([^"]+)", role: "dialog"', picker_js)
check("path-picker.js builds a backdrop element around its card (found by reading it)", bool(m_back) and bool(m_card))
back_cls = m_back.group(1) if m_back else "?"
card_cls = m_card.group(1) if m_card else "?"
check("path-picker.js really appends that backdrop to the page", "appendChild(backdrop)" in picker_js and "(document.body || document.documentElement).appendChild(backdrop)" in picker_js)
check("path-picker.css styles those same two classes", ("." + back_cls) in picker_css and re.search(r"\.%s\s*\{" % re.escape(card_cls), picker_css) is not None)
m_sel = re.search(r'const PICKER_SELECTOR = "([^"]+)";', js)
check("the panel has ONE picker selector constant", bool(m_sel) and js.count("PICKER_SELECTOR =") == 1)
tokens = [t.strip() for t in (m_sel.group(1) if m_sel else "").split(",")]
check("the panel's selector names the picker's backdrop (the class path-picker.js adds)", ("." + back_cls) in tokens, (tokens, back_cls))
check("the panel's selector names the picker's card too", ("." + card_cls) in tokens, (tokens, card_cls))
check("the classes the panel once guessed (.pp-overlay, .path-picker, pp-backdrop) are gone from the panel",
      re.search(r"(?<![\w-])pp-(?:overlay|backdrop)|\.path-picker\b", js) is None)
check("the picker's classes are not defined by the panel's own stylesheet (they are the picker's)", back_cls not in css and ("." + card_cls + "{") not in css)
on_key = js[js.index("S.onKey = (e) => {"):js.index('document.addEventListener("keydown", S.onKey, true);')]
check("the panel's key handler stands aside for the picker BEFORE it reads Escape or Tab",
      "pickerOpen(document)" in on_key and on_key.index("pickerOpen(document)") < on_key.index('e.key === "Escape"') < on_key.index('e.key !== "Tab"'), on_key[:400])
check("...and for a confirmation dialog, as before", on_key.index("#jui-layer .jui-modal") < on_key.index("pickerOpen(document)"))
check("the picker still handles its own Escape and Tab (the panel relies on it for the keys inside the card)",
      'e.key === "Escape"' in picker_js and 'e.key === "Tab"' in picker_js and 'addEventListener("keydown", onKey, true)' in picker_js)

# 12b. Mixed pack lists: the tool must still allow what the panel sends.
pk_src = read(ROOT / "jarvis-cli" / "jarvis" / "actions" / "minecraft_packs.py")
cm_src = read(ROOT / "jarvis-cli" / "jarvis" / "modhosts" / "crossmatch.py")
check("the pack tool takes a per-entry source and says it is for entries_from", '"source": {"type": "string", "enum": ["modrinth", "curseforge"]' in pk_src and "only with entries_from" in pk_src)
check("crossmatch decides what to match PER ENTRY: an entry's own source, else entries_from, against the pack's platform",
      'foreign = [i for i, e in enumerate(entries) if (e["source"] or origin) != target]' in cm_src)
check("crossmatch keeps the entries already on the pack's platform (a mixed list still has native entries to build from)",
      "a mixed list still has its native entries to build from" in cm_src and "rebuilt.append(_raw_entry(parsed))" in cm_src)
check("the pack builder skips an entry whose source is not its platform (so a stray source cannot be matched silently)", '"wrong_platform"' in read(ROOT / "jarvis-cli" / "jarvis" / "modhosts" / "packs.py"))
check("the panel: every mixed entry's source is written in ONE place (packArgs), never one tool call per platform",
      js.count("e.source = args.format") == 1)
check("the panel: a mixed pack names entries_from itself (the other platform), not from the form",
      "const from = mixed ? otherPlatform(args.format) : oneOf(f.entries_from, PLATFORMS, \"\");" in js)
check("the panel's pack tab offers 'Both' for the tray's platforms and still lets the owner use one platform only",
      '["both", "Both: each project on its own platform"]' in js and '["modrinth", "Modrinth only"]' in js and '["curseforge", "CurseForge only"]' in js)
check("the panel's pack tab shows each entry with its platform badge and says what happens to it", "mc-pack-entry" in js and "goes in as it is" in js and ".mc-pack-entry" in css)
check("the mixed list's styles use the shared tokens only (no hex or rgba)", not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css[css.index("A mixed pack: each entry"):css.index("A mixed pack: each entry") + 900]))

# 12c. Scroll into view.
check("the Pack tab brings its result (and its one-line problem) into view", js.count("reveal(host);") == 2, js.count("reveal(host);"))
check("the OptiFine tab brings its list and its download result into view", js.count("reveal(listHost);") == 1 and js.count("reveal(dlHost);") == 3, (js.count("reveal(listHost);"), js.count("reveal(dlHost);")))
check("the tray still brings its own result into view, as before", "results.scrollIntoView({ block: \"nearest\" })" in js)

# 12d. the round-10 fixtures
H10 = ["hand_10a_pack_mixed_dry.json", "hand_10a_pack_mixed_needs_download.json", "hand_10a_pack_mixed_written.json"]
h10 = sorted(p.name for p in DATA.glob("hand_10a_*.json"))
check("the hand_10a_ fixtures are all there (git add -f: tests/data is git-ignored)", h10 == H10, sorted(set(H10) ^ set(h10)))
for name in h10:
    try:
        ok = isinstance(json.loads(read(DATA / name)), dict)
    except Exception:  # noqa: BLE001
        ok = False
    check("%s parses" % name, ok)
check("no round-10 fixture holds anything that looks like a key", not any(re.search(r"[A-Za-z0-9_\-]{32,}", read(DATA / n)) for n in h10))

# Run the checks that need another agent's patch last, so a missing patch is one clearly named group of failures.
test_needs_m1_update_source_tools()
test_needs_m2_undo_tools()

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
