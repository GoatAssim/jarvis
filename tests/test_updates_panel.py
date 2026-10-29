"""The Updates Menu panel (Download Hub round 5, agent H2): files, wiring, and the rules its code must keep.

Static and declarative: nothing here opens a browser, a network, a daemon or the hub.  It READS the panel's files.
The behaviour of the pure helpers and of the write flows (dry run -> one dialog -> apply; the master switch asks first; only
the allowed setting keys are sent) is checked by tests/verify_updates_panel.js against a fake hub built from the contract.

Covers:
  1. The files exist and are wired: the Menu item (with its badge), the stylesheet, the script, the click handler and the
     JarvisHost.openPanel entry.
  2. The palette registry: an `updates` verb in the panels group that opens that Menu item, covers no CLI name, and is a panel
     verb in slash-palette.js; the palette test reads its counts from the registry instead of typing them.
  3. The code rules: only textContent / setAttribute (no innerHTML and friends), no browser storage, no native dialogs, one
     fetch and it carries the hub header, only the six hub routes, no links, no version ordering, no colours of its own.
  4. The order inside the write flows (a dry run before the dialog, the dialog before the apply, the master-switch dialog
     before its write) and the shape of the panel (a real dialog: aria-modal, Escape, Tab kept inside, focus restored).
  5. Every class the script uses has a rule, and every token the stylesheet reads is defined in style.css.
  6. settings.js is untouched by this panel (its switches live in the panel).
Run: python tests/test_updates_panel.py        (not run by the agent that wrote it)
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-updates-panel-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "web" / "public"
PASS, FAIL = [], []


def check(name, ok, extra=None):
    (PASS if ok else FAIL).append(name)
    if not ok:
        print("FAIL: %s%s" % (name, "  -> %r" % (extra,) if extra is not None else ""))


def read(p):
    return Path(p).read_text(encoding="utf-8")


def code_only(text):
    """The file without its comments (block comments and whole-line or trailing // comments), so the rules below read the code."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)(^|\s)//\s.*$", "", text)


js_path, css_path = PUBLIC / "updates-panel.js", PUBLIC / "updates-panel.css"
check("the panel's script, stylesheet and both tests exist",
      js_path.exists() and css_path.exists() and (ROOT / "tests" / "verify_updates_panel.js").exists())
js_raw, css_raw = read(js_path), read(css_path)
js, css = code_only(js_raw), code_only(css_raw)
index, app, palette = read(PUBLIC / "index.html"), read(PUBLIC / "app.js"), read(PUBLIC / "slash-palette.js")

# --- 1. wiring ------------------------------------------------------------------------------------
check("index.html has the Menu item (a menuitem button) with its number badge inside the label",
      re.search(r'<button[^>]*id="menu-item-updates"[^>]*role="menuitem"', index) is not None
      and re.search(r'id="menu-item-updates-badge"[^>]*hidden', index) is not None and ">Updates<span" in index)
check("the stylesheet and the script are linked, the script after the other panel scripts",
      '<link rel="stylesheet" href="updates-panel.css">' in index and '<script src="updates-panel.js"></script>' in index
      and index.index("minecraft-panel.js") < index.index("updates-panel.js"))
check("the Menu click closes the menu and opens the panel", re.search(r'#menu-item-updates"\)\?\.addEventListener\("click", \(\) => \{ closePanelMenu\(\); window\.JarvisUpdates\?\.open\(\); \}\)', app) is not None)
check("JarvisHost.openPanel knows `updates`", "updates: () => window.JarvisUpdates?.open()" in app)
check("the panel exports open, close and refreshBadge", all(w in js for w in ("global.JarvisUpdates = {", "open, close, refreshBadge")))

# --- 2. the palette ---------------------------------------------------------------------------------
reg = json.loads(re.search(r"/\*JSON-BEGIN\*/(.*)/\*JSON-END\*/", read(PUBLIC / "slash-commands-data.js"), re.S).group(1))
verbs = {v["verb"]: v for v in reg["verbs"]}
v = verbs.get("updates", {})
check("the registry has an `updates` verb in the panels group that opens that Menu item",
      v.get("group") == "panels" and v.get("menu") == "menu-item-updates" and v.get("riskTier") == "safe" and v.get("example") == "/updates", v)
check("it covers no CLI name (the panel has no CLI subcommand, so nothing goes into reserved_names.py)", v.get("covers") == [] and v.get("args") == [] and v.get("aliases") == [])
reserved = read(ROOT / "jarvis-cli" / "jarvis" / "reserved_names.py")
check("`updates` is not a reserved CLI name", not re.search(r'["\']updates["\']', reserved))
check("slash-palette.js lists it as a panel verb", re.search(r'PANEL_VERBS = new Set\(\[[^\]]*"minecraft", "updates"\]', palette) is not None)
pal_test = read(ROOT / "tests" / "verify_slash_palette.js")
check("the palette test reads its verb counts from the registry (no typed 41 / 42 / 30)",
      "SHOWN_VERBS" in pal_test and not re.search(r"=== 4[12]\b", pal_test) and "=== 41 + 32 + 30" not in pal_test)

# --- 3. the code rules ----------------------------------------------------------------------------
check("nothing another program said can become markup: no innerHTML, outerHTML, insertAdjacentHTML, document.write, eval or new Function",
      not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function", js))
check("text goes in with textContent / createTextNode / setAttribute", "textContent" in js and "createTextNode" in js and "setAttribute" in js)
check("nothing is kept in the browser", not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie", js))
check("no native dialogs (the page's own dialog is used)", not re.search(r"\b(window\.)?(confirm|alert|prompt)\(", js) and "JarvisUI" in js)
check("one fetch call, and every write carries the header the server requires",
      js.count("fetch(") == 1 and '"X-Jarvis-Hub": "1"' in js and 'method: "POST"' in js)
routes = set(re.findall(r'"(/api/[a-z0-9/\-]+)"', js))
check("only the six hub routes are named: the three that existed and the three new ones",
      routes == {"/api/hub/updates", "/api/hub/updates/check", "/api/hub/updates/dismiss", "/api/hub/updates/apply", "/api/hub/updates/dry-run", "/api/hub/updates/set"}, routes)
check("the one read is a GET with no body and every other route is a POST through the same helper",
      "ROUTES.read" in js and js.count("ROUTES.read") == 1 and "body === undefined ? {}" in js)
check("no link of any kind (the hub hands over no link, so none is drawn)", not re.search(r"href|https?://|\"a\"|<a\b|window\.open|location\.", js))
check("no price, no rating, no review", not re.search(r"\b(price|rating|stars?|reviews?)\b", js, re.I))
check("the panel never calls two versions newer or older (the hub does not compare them)", not re.search(r'"[^"\n]*\b(newer|older|latest|outdated)\b[^"\n]*"', js, re.I))
check("rows and blocks are ordered by name: no sort mentions a version", all(not re.search(r"installed|available|version", ln) for ln in js.splitlines() if ".sort(" in ln))
check("the stylesheet writes no colour of its own (only the skin tokens)", not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css) and "var(--" in css)

# --- 4. the order of the write flows and the shape of the panel -------------------------------------------
one = js[js.index("async function updateOne"):js.index("async function updateMany")]
check("one update: the dry run, then the dialog showing the plan, then the real apply (in that order)",
      one.index("ROUTES.dryRun") < one.index("d.ask(confirmOneText") < one.index("applyRequest({"), None)
many = js[js.index("async function updateMany"):js.index("async function applyRequest")]
check("Update all: one dialog before the one apply, nothing at all when there is nothing to update",
      many.index("planAll(") < many.index("d.ask(confirmAllText") < many.index("applyRequest(plan.args)") and "if (!plan.count)" in many)
auto = js[js.index("async function switchAuto"):js.index("async function setSetting")]
check("the master switch: the dialog before its write, and only switching ON asks", auto.index("d.ask(AUTO_DIALOG)") < auto.index('setSetting("global"') and "if (on &&" in auto)
check("the master-switch dialog tells what it does, what it touches, that failures are not retried, and that it is off by default",
      all(w in js for w in ("starts a source\\u2019s own updater by itself", "only touches sources that have their own", "not retried by itself", "off by default")))
check("a dry run that fails starts nothing", "stage: \"dry_run\"" in one and one.index('stage: "dry_run"') < one.index("d.ask("))
check("with no page dialog available nothing runs (a yes must come from a person)", "if (!ui || typeof ui.confirm !== \"function\") return false;" in js)
check("the panel can only write the three scopes' own keys", all(w in js for w in ('global: ["auto_update"]', 'group: ["included"]', 'provider: ["update_checks", "auto_update"]')))
check("Update buttons are real <button>s that are disabled (not hidden) when there is no updater or one is already running",
      "disabled: !can || busy" in js and 'NO_UPDATER' in js)
check("it is a real modal dialog: role=dialog, aria-modal, a label, Escape closes, Tab stays inside, focus goes back",
      all(w in js for w in ('role: "dialog"', '"aria-modal": "true"', '"aria-label": "Updates"', 'e.key === "Escape"', 'e.key !== "Tab"', "S.prevFocus", "back.focus()")))
check("a confirmation owns the keys while it is open (Escape does not close the panel under it)", "#jui-layer .jui-modal" in js)
check("a redraw keeps the focused control and the scroll positions", "data-focus" in js and "scrollTop" in js)
check("the list is polled quickly while an apply runs and slowly otherwise; a hidden tab reads nothing", all(w in js for w in ("POLL = {", "document.hidden", "applying: 3000", "closed: 60000")))
check("all the text on the page is built with el(), which uses textContent (no template of markup anywhere)", "node.textContent = text(v)" in js and not re.search(r"`[^`]*<[a-z]+[ >][^`]*`", js))
check("the banner for a stopped daemon names where to start it", "Start it in Menu > Daemons" in js)

# --- 5. classes and tokens --------------------------------------------------------------------------
cls_re = r"up-[a-z0-9]+(?:-[a-z0-9]+)*(?:__[a-z0-9]+(?:-[a-z0-9]+)*)?"
used, styled = set(re.findall(cls_re, js)), set(re.findall(r"\.(" + cls_re + ")", css))
check("every `up-` class the script uses has a rule in the stylesheet", used <= styled, sorted(used - styled))
style = read(PUBLIC / "style.css")
tokens_used, tokens_def = set(re.findall(r"var\((--[a-z0-9-]+)", css)), set(re.findall(r"(--[a-z0-9-]+)\s*:", style))
check("every token the stylesheet reads is defined in style.css", tokens_used <= tokens_def, sorted(tokens_used - tokens_def))
check("it uses the shared panel chrome (debug-overlay, debug-panel, debug-body) like the Scheduled panel", all(w in js for w in ("debug-overlay", "debug-panel", "debug-body", "debug-pane")))
check("the narrow-window layout stacks the three parts", "@media (max-width: 900px)" in css and "grid-template-columns: 1fr" in css)

# --- 6. the panel's switches live in the panel ------------------------------------------------------
settings = read(PUBLIC / "settings.js")
check("settings.js is untouched by this panel (no reference to it)", "JarvisUpdates" not in settings and "updates-panel" not in settings)
check("the browser files carry no `updates` panel verb twice", sum(1 for x in reg["verbs"] if x["verb"] == "updates") == 1)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
