"""The Minecraft Menu panel and its palette commands (wave S3).

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
  6. The hand-written hand_s3_* fixtures exist and parse.

Run: python3 tests/test_minecraft_panel.py        (written by the S3 agent; not run by it)
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
from jarvis.actions import minecraft_download, minecraft_optifine, minecraft_packs  # noqa: E402

declared = set()
for mod in (lookup, minecraft_download, minecraft_optifine, minecraft_packs):
    declared |= {s["name"] for s in mod.TOOL_SCHEMAS}
check("the four action files declare exactly the six tools", declared == SIX, declared)
js = read(JS)
m = re.search(r"const TOOLS = \[([^\]]*)\]", js)
panel_tools = set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()
check("the panel's own list matches them", panel_tools == SIX, panel_tools)
used = set(re.findall(r'runTool\("([a-z_]+)"', js))
check("every tool the panel runs is one of the six", used and used <= SIX, used)

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
check("a real write is always confirmed (sure: true on queue, OptiFine and pack)", code.count("sure: true") == 3)
css = read(CSS)
check("the stylesheet uses only :root tokens for colour", re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css) is None)
check("the stylesheet is scoped to the panel", all(sel.strip().startswith((".mc-", ".menu-panel--minecraft", "@media", "}")) or not sel.strip() or sel.strip().startswith(("*", "/*", " *"))
      for sel in re.findall(r"^([^\s{][^{]*)\{", css, re.M)))

# --- 6. fixtures -----------------------------------------------------------------------------
DATA = ROOT / "tests" / "data" / "modhosts"
fixtures = sorted(p.name for p in DATA.glob("hand_s3_*.json"))
check("the hand_s3_ fixtures are all there", fixtures == ["hand_s3_download_dry.json", "hand_s3_hostile.json", "hand_s3_not_confirmed.json",
                                                         "hand_s3_project_files.json", "hand_s3_search_both_nokey.json"], fixtures)
for name in fixtures:
    try:
        data = json.loads(read(DATA / name))
        ok = isinstance(data, dict)
    except Exception:  # noqa: BLE001
        ok = False
    check("%s parses" % name, ok)
check("no fixture holds anything that looks like a key", not any(re.search(r"[A-Za-z0-9_\-]{32,}", read(DATA / n)) for n in fixtures))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
