"""Part E.5 / K.2.5.3 (reworked rev. 2026-10-10): the Console filter lives in the
Ask Jarvis header now, not on the Live Feed panel.

The category list exists in three places that have no shared source:
  web/public/app.js   ASK_CONSOLE_CATEGORIES   (what the menu offers)
  web/server.js       ASK_CONSOLE_KINDS        (what POST /api/ask-console-filter accepts)
  web/public/style.css one [data-ask-hidden~=..] rule per category

and the classifier keys off the exact line shapes jarvis-cli prints on stderr.
If any of these drift apart a category silently stops hiding anything (or a saved
preference is dropped by the server). This test reads all of them straight from
source and fails on drift.

If it fails: change the lists to match in the same change.
Run: python3 tests/test_ask_console_filter.py
Companion (classifier behaviour, markup, CSS): node tests/verify_ask_console_filter.js
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "web" / "public" / "app.js").read_text(encoding="utf-8")
SERVER = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "public" / "style.css").read_text(encoding="utf-8")
CLI = (ROOT / "jarvis-cli" / "jarvis" / "cli.py").read_text(encoding="utf-8")
TOOL_LOADER = (ROOT / "jarvis-cli" / "jarvis" / "tool_loader.py").read_text(encoding="utf-8")
TOKEN_USAGE = (ROOT / "jarvis-cli" / "jarvis" / "token_usage.py").read_text(encoding="utf-8")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def app_categories():
    m = re.search(r"const ASK_CONSOLE_CATEGORIES = \[(.*?)\n  \];", APP, re.S)
    return re.findall(r'\{\s*id:\s*"([^"]+)"', m.group(1)) if m else []


def server_kinds():
    m = re.search(r"const ASK_CONSOLE_KINDS = \[(.*?)\];", SERVER, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def css_kinds():
    return re.findall(r'\[data-ask-hidden~="([^"]+)"\] \.ask-prompt-line\[data-kind="\1"\]', CSS)


app_ids, srv_ids, css_ids = app_categories(), server_kinds(), css_kinds()
check("app.js lists categories", len(app_ids) >= 8, app_ids)
check("server.js accepts exactly the categories app.js offers", sorted(app_ids) == sorted(srv_ids), (app_ids, srv_ids))
check("style.css has one hide rule per category", sorted(app_ids) == sorted(css_ids), (app_ids, css_ids))
check("category ids are unique", len(set(app_ids)) == len(app_ids), app_ids)
check("the owner's three examples are categories (tool calls, token cost, [tools])",
      {"tool-call", "tokens", "discovery"} <= set(app_ids), app_ids)

# --- server endpoint -------------------------------------------------------
check("server exposes GET and POST /api/ask-console-filter",
      'app.get("/api/ask-console-filter"' in SERVER and 'app.post("/api/ask-console-filter"' in SERVER)
check("old Live Feed endpoint /api/console-filter-prefs is gone (no routes)",
      'app.get("/api/console-filter-prefs"' not in SERVER and 'app.post("/api/console-filter-prefs"' not in SERVER)
check("server drops unknown kinds on read and on write",
      SERVER.count("ASK_CONSOLE_KINDS.includes(k)") >= 2)
check("preference is a file under web/data (gitignored), not localStorage",
      '"ask-console-filter.json"' in SERVER and "localStorage" not in APP[APP.index("ASK_CONSOLE_CATEGORIES"):APP.index("function askConsoleCounts")])

# --- the classifier keys off these exact CLI line shapes -------------------
check("cli.py still prints '↳ asking <label>…' (provider)", "\\u21b3 asking " in CLI)
check("cli.py still prints '$ routed: …' (discovery)", "$ routed: " in CLI)
check("cli.py still prints '  » text' (narration)", "\\u00bb {text}" in CLI)
check("cli.py still prints '  ✗ label — err' (error)", "\\u2717 {label}" in CLI)
check("cli.py still prints 'tokens: in=' (tokens)", "tokens: in=" in CLI)
check("cli.py still prints '(note: …)' (status)", "(note: " in CLI)
check("cli.py still prints '  $ <tool> …' (tool-call)", 'f"{ERR.DIM}  $ {friendly}{detail}{ERR.RESET}"' in CLI)
check("token_usage.format_ask_total still starts 'ask total='", '"ask total=%d' in TOKEN_USAGE)
check("tool_loader still logs '[tools] …' lines", TOOL_LOADER.count('"[tools] ') + TOOL_LOADER.count("[tools] ") >= 3)

# --- removal of the old Live Feed filter -----------------------------------
html = (ROOT / "web" / "public" / "index.html").read_text(encoding="utf-8")
check("Live Feed filter markup removed from index.html", 'id="console-filter"' not in html and 'id="console-search"' not in html)
check("Console picker present in the Ask header", 'id="btn-console-filter"' in html)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
