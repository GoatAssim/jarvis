"""L.33 - the Tool Manager's code editor, checked in a real browser.

Loads web/public/index.html with the real ui-kit.js / tool-manager.js /
code-editor.js / CSS in headless Chromium and answers /api/tools, /api/ctools/*
from an in-memory fake, so no Jarvis CLI or Node server is needed. Covers:
syntax colours on screen, the painted layer lining up with the caret's text,
smart Enter / Tab / auto-close / comment toggle / move line, undo after those
edits, the completion popup and snippets, inline ghost suggestions (shown after
a pause, Tab accepts, Esc dismisses, typing along keeps them, a request is not
made for a mid-line caret, switching them off sends nothing), the per-session
cap, scroll sync between text, painted layer and line numbers, the error-line
marker, the outline, and that Save sends exactly what is in the editor.

It does NOT exercise web/server.js or the Python side (test_custom_tools_suggest
covers the latter) - only the panel's side of the contract.

Needs the `playwright` Python package and a launchable Chromium; prints SKIP
and exits 0 without them, like verify_l9_sequence_bar.js.

    python3 tests/verify_l33_code_editor.py
"""
import json, mimetypes, sys
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

PUB = Path(__file__).resolve().parent.parent / "web" / "public"
MINIMAL = '"""My custom tool."""\n\n\ndef tool_hello(args):\n    args = args or {}\n    who = (args.get("name") or "world").strip()\n    return {"ok": True, "greeting": "Hello, %s!" % who}\n\n\nTOOL_GROUP = "custom"\n'

state = {"suggest_calls": [], "suggest_reply": "return {\"ok\": True}", "writes": [], "check_error": None}

def handle(route):
    req = route.request
    url = req.url
    path = "/" + (url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url).split("?")[0]
    def js(obj, status=200):
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(obj))
    if path == "/api/tools": return js({"tools": []})
    if path == "/api/commands": return js({"commands": {}})
    if path == "/api/disabled": return js({"tools": [], "commands": []})
    if path == "/api/ctools": return js({"tools": []})
    if path == "/api/ctools/templates": return js({"templates": [{"id": "minimal", "label": "Minimal", "hint": ""}]})
    if path == "/api/ctools/draft": return js({"ok": True, "source": MINIMAL})
    if path.startswith("/api/ctools/") and path.endswith("/suggest"):
        body = json.loads(req.post_data or "{}")
        state["suggest_calls"].append(body)
        return js({"ok": True, "text": state["suggest_reply"], "provider": "fake"})
    if path.startswith("/api/ctools/") and path.endswith("/check"):
        if state["check_error"]:
            return js(dict(ok=False, stage="syntax", error=state["check_error"]))
        return js({"ok": True, "stage": "ok", "tools": ["hello"], "group": "custom"})
    if path.startswith("/api/ctools/") and req.method == "PUT":
        body = json.loads(req.post_data or "{}"); state["writes"].append(body.get("source"))
        return js({"ok": True, "note": "ok", "tools": ["hello"]})
    if path.startswith("/api/"): return js({})
    f = PUB / path.lstrip("/")
    if path in ("/", ""): f = PUB / "index.html"
    if f.is_file():
        return route.fulfill(status=200, content_type=mimetypes.guess_type(str(f))[0] or "text/plain", body=f.read_bytes())
    return route.fulfill(status=404, body="")

res = []
def check(name, cond, detail=""):
    res.append((name, bool(cond)))
    print(("ok   " if cond else "FAIL ") + name + ((": " + str(detail)) if (detail and not cond) else ""))

def settle(pred, ms=3000):
    for _ in range(ms // 50):
        if pred(): return True
        pg.wait_for_timeout(50)
    return pred()

with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception as e:  # noqa: BLE001
        print("SKIP: Chromium could not be launched:", str(e)[:120]); sys.exit(0)
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/*", handle)
    pg.goto("http://jarvis.test/")
    pg.wait_for_function("window.JarvisToolManager && window.JarvisCodeEditor")
    pg.evaluate("JarvisToolManager.open({create: true})")
    pg.wait_for_selector(".ce__ta")
    pg.wait_for_timeout(400)

    ta = pg.locator(".ce__ta")
    val = lambda: pg.evaluate("document.querySelector('.ce__ta').value")
    sel = lambda: pg.evaluate("[document.querySelector('.ce__ta').selectionStart, document.querySelector('.ce__ta').selectionEnd]")
    def set_text(t, caret=None):
        pg.evaluate("""([t, c]) => { const e = JarvisToolManager._editor ? null : null; }""", [t, caret])
    def clear_editor():
        ta.focus(); pg.keyboard.press("Control+A"); pg.keyboard.press("Delete")

    # ---- the template loaded and is painted ------------------------------------
    check("template source is in the editor", "tool_hello" in val())
    check("keywords are coloured", pg.locator(".ce__hl .ce-t-kw").count() >= 3)
    check("function name is coloured as a definition", pg.locator(".ce__hl .ce-t-fn-def").first.inner_text() == "tool_hello")
    check("strings and the docstring are coloured", pg.locator(".ce__hl .ce-t-string").count() >= 3)
    check("the painted text is the typed text", pg.evaluate("document.querySelector('.ce__hl code').textContent") == val() + "\n")
    cs = pg.evaluate("""() => { const a = getComputedStyle(document.querySelector('.ce__ta')), b = getComputedStyle(document.querySelector('.ce__hl'));
      return ['fontFamily','fontSize','lineHeight','letterSpacing','tabSize','paddingLeft','paddingTop'].map(k => a[k] === b[k]); }""")
    check("textarea and painted layer share font, size, line height and padding", all(cs), cs)
    check("textarea text is transparent", pg.evaluate("getComputedStyle(document.querySelector('.ce__ta')).webkitTextFillColor") in ("rgba(0, 0, 0, 0)", "transparent"))
    # a character's painted position lines up with where the caret sits
    cw = pg.evaluate("document.querySelector('.ce__measure').getBoundingClientRect().width / 20")
    w = pg.evaluate("""() => { const c = document.querySelector('.ce__hl code'); const r = document.createRange(); r.selectNodeContents(c); return r.getBoundingClientRect().width; }""")
    longest = max(len(l) for l in MINIMAL.split("\n"))
    check("painted width equals char width x longest line", abs(w - cw * longest) < 2.0, (w, cw * longest))
    check("line numbers match the line count", pg.locator(".ce__gutter-inner span").count() == len(MINIMAL.split("\n")))
    pg.screenshot(path="/tmp/l33_template.png")

    # ---- editing keys ------------------------------------------------------------
    clear_editor()
    pg.keyboard.type("def f(")
    check("typing ( auto-closes", val() == "def f()" and sel() == [6, 6], (val(), sel()))
    pg.keyboard.type("a")
    pg.keyboard.press("ArrowRight")
    pg.keyboard.type(":")
    pg.keyboard.press("Enter")
    check("Enter after a colon indents", val() == "def f(a):\n    " and sel() == [14, 14], repr(val()))
    pg.keyboard.type("return (1")
    check("( auto-closes inside the body", val().endswith("return (1)"), repr(val()))
    pg.keyboard.type(")")
    check("typing ) types over the closer", val().endswith("return (1)") and not val().endswith("))"), repr(val()))
    pg.keyboard.press("Enter")
    check("Enter after return dedents", val().endswith("return (1)\n"), repr(val()))
    pg.keyboard.press("Control+z")
    check("undo reverts the smart Enter in one step", val().endswith("return (1)"), repr(val()))
    pg.keyboard.press("Control+z")
    pg.keyboard.press("Control+z")
    check("undo keeps working through earlier edits", len(val()) < len("def f(a):\n    return (1)"), repr(val()))
    clear_editor()
    pg.keyboard.type('x = "hi')
    check("quotes auto-close", val() == 'x = "hi"', repr(val()))
    clear_editor()
    pg.keyboard.type("it")
    if pg.locator(".ce__pop:not([hidden])").count(): pg.keyboard.press("Escape")
    pg.keyboard.type("'s")
    check("an apostrophe after a word does not pair", val() == "it's", repr(val()))
    clear_editor()
    pg.keyboard.type("a = 1\nb = 2")
    pg.keyboard.press("Control+a")
    pg.keyboard.press("Control+/")
    check("Ctrl+/ comments the selected lines", val() == "# a = 1\n# b = 2", repr(val()))
    pg.keyboard.press("Control+/")
    check("Ctrl+/ again uncomments", val() == "a = 1\nb = 2", repr(val()))
    pg.keyboard.press("Control+Home")
    pg.keyboard.press("Alt+ArrowDown")
    check("Alt+Down moves the line", val() == "b = 2\na = 1", repr(val()))
    pg.keyboard.press("Shift+Alt+ArrowDown")
    check("Shift+Alt+Down copies the line", val().count("a = 1") == 2, repr(val()))
    clear_editor()
    pg.keyboard.type("if x:")
    pg.keyboard.press("Enter")
    pg.keyboard.type("y")
    pg.keyboard.press("Shift+Tab")
    check("Shift+Tab outdents", val() == "if x:\ny", repr(val()))
    pg.keyboard.press("Tab")
    check("Tab at the caret inserts spaces up to the next stop", val() == "if x:\ny   ", repr(val()))
    pg.keyboard.press("Backspace")
    check("Backspace after text is an ordinary delete", val() == "if x:\ny  ", repr(val()))
    pg.keyboard.press("Home"); pg.keyboard.type("    ")
    pg.keyboard.press("Backspace")
    check("Backspace in indentation removes a whole stop", val() == "if x:\ny  ", repr(val()))

    # ---- bracket match + current line ---------------------------------------------
    clear_editor()
    pg.keyboard.type("print(len(x))")
    pg.keyboard.press("ArrowLeft")
    check("the bracket pair next to the caret is highlighted", pg.locator(".ce__hl .ce-t-bm").count() == 2, pg.locator(".ce__hl .ce-t-bm").count())
    check("the status bar reports line and column", "Ln 1, Col 13" in pg.locator(".ce__status .ce__stat").first.inner_text(), pg.locator(".ce__status .ce__stat").first.inner_text())

    # ---- completion popup ------------------------------------------------------------
    clear_editor()
    pg.keyboard.type("jt")
    check("typing two characters opens the completion popup", settle(lambda: pg.locator(".ce__pop:not([hidden])").count() == 1))
    check("the snippet is the first suggestion", "jtool" in pg.locator(".ce__pop-item").first.inner_text())
    pg.keyboard.press("Tab")
    check("Tab inserts the snippet", val().startswith("def tool_name(args):") and "return {" in val(), repr(val()[:60]))
    check("the popup closes after accepting", pg.locator(".ce__pop:not([hidden])").count() == 0)
    check("the inserted snippet is not indented twice", "\n    args = args or {}" in val(), repr(val()[:80]))
    clear_editor()
    pg.keyboard.type("ret")
    pg.keyboard.press("Escape")
    check("Esc closes the popup", pg.locator(".ce__pop:not([hidden])").count() == 0)
    pg.keyboard.press("Control+Space")
    check("Ctrl+Space reopens it", pg.locator(".ce__pop:not([hidden])").count() == 1)
    pg.keyboard.press("ArrowDown")
    pg.keyboard.press("Escape")

    # ---- inline (ghost) suggestions -------------------------------------------------------
    pg.evaluate("localStorage.removeItem('jarvis-tool-manager-ui')")
    clear_editor()
    state["suggest_calls"].clear()
    state["suggest_reply"] = '{"ok": True, "result": value}'
    pg.keyboard.type("def tool_a(args):")
    pg.keyboard.press("Enter")
    pg.keyboard.type("value = args.get('v')")
    pg.keyboard.press("Enter")
    pg.keyboard.type("return ")
    check("no request while still typing", len(state["suggest_calls"]) == 0)
    check("a request is made after a pause", settle(lambda: len(state["suggest_calls"]) == 1, 4000), len(state["suggest_calls"]))
    check("the request carries the source and the caret offset", state["suggest_calls"] and state["suggest_calls"][0]["cursor"] == len(val()) and state["suggest_calls"][0]["source"] == val())
    check("the suggestion appears dimmed after the caret", settle(lambda: pg.locator(".ce__ghost--first").is_visible()) and pg.locator(".ce__ghost--first").inner_text() == state["suggest_reply"])
    gx = pg.evaluate("document.querySelector('.ce__ghost--first').getBoundingClientRect().left")
    lx = pg.evaluate("""() => { const c = document.querySelector('.ce__hl code'); const r = document.createRange(); const t = c.lastChild; return 0; }""")
    check("the status bar says Tab accepts", "Tab to accept" in pg.locator(".ce__sug").inner_text())
    pg.keyboard.press("Tab")
    check("Tab accepts the suggestion", val().endswith("return " + state["suggest_reply"]), repr(val()[-40:]))
    check("the ghost is gone after accepting", not pg.locator(".ce__ghost--first").is_visible())
    pg.screenshot(path="/tmp/l33_after_accept.png")

    # Esc dismisses, and a new request is not made for the same spot
    clear_editor()
    state["suggest_calls"].clear()
    pg.keyboard.type("def tool_b(args):")
    pg.keyboard.press("Enter")
    pg.keyboard.type("value = 1")
    settle(lambda: pg.locator(".ce__ghost--first").is_visible(), 4000)
    pg.keyboard.press("Escape")
    check("Esc dismisses the suggestion", not pg.locator(".ce__ghost--first").is_visible())
    # typing along keeps the rest on screen
    clear_editor()
    state["suggest_calls"].clear()
    state["suggest_reply"] = "return value"
    pg.keyboard.type("def tool_c(args):")
    pg.keyboard.press("Enter")
    pg.keyboard.type("value = 1")
    pg.keyboard.press("Enter")
    settle(lambda: pg.locator(".ce__ghost--first").is_visible(), 4000)
    pg.keyboard.type("ret")
    check("typing the suggested letters keeps the remainder", pg.locator(".ce__ghost--first").is_visible() and pg.locator(".ce__ghost--first").inner_text() == "urn value", pg.locator(".ce__ghost--first").inner_text())
    pg.keyboard.type("x")
    check("typing something else drops it", not pg.locator(".ce__ghost--first").is_visible())
    # Ctrl+Right takes one word
    clear_editor()
    state["suggest_reply"] = "return value + 1"
    pg.keyboard.type("def tool_d(args):")
    pg.keyboard.press("Enter")
    pg.keyboard.type("value = 1")
    pg.keyboard.press("Enter")
    settle(lambda: pg.locator(".ce__ghost--first").is_visible(), 4000)
    pg.keyboard.press("Control+ArrowRight")
    check("Ctrl+Right accepts one word", val().endswith("return") and pg.locator(".ce__ghost--first").inner_text().startswith(" value"), (val()[-20:], pg.locator(".ce__ghost--first").inner_text()))

    # not mid-line
    clear_editor()
    pg.keyboard.type("def tool_e(args):\n    value = foo(1) + bar(2)\n    other = 3")
    if pg.locator(".ce__pop:not([hidden])").count(): pg.keyboard.press("Escape")
    state["suggest_calls"].clear()
    pg.keyboard.press("ArrowUp"); pg.keyboard.press("End"); pg.keyboard.press("ArrowLeft"); pg.keyboard.press("ArrowLeft")
    pg.keyboard.type("z")
    pg.wait_for_timeout(1500)
    check("no request when the caret is mid-line", len(state["suggest_calls"]) == 0, len(state["suggest_calls"]))

    # off switch
    pg.locator(".ce__sug").click()
    check("the toggle turns suggestions off", "off" in pg.locator(".ce__sug").inner_text().lower())
    check("the choice is remembered", json.loads(pg.evaluate("localStorage.getItem('jarvis-tool-manager-ui')")).get("suggest") is False)
    state["suggest_calls"].clear()
    clear_editor()
    pg.keyboard.type("def tool_f(args):\n    value = 1\n    return value")
    pg.wait_for_timeout(1700)
    check("with suggestions off nothing is sent", len(state["suggest_calls"]) == 0)
    pg.keyboard.press("Alt+\\")
    check("Alt+\\ asks anyway", settle(lambda: len(state["suggest_calls"]) == 1, 3000), len(state["suggest_calls"]))
    pg.locator(".ce__sug").click()

    # ---- scrolling -----------------------------------------------------------------------------
    long = "\n".join("value_%d = %d" % (i, i) for i in range(200)) + "\n"
    pg.evaluate("""(t) => { const ta = document.querySelector('.ce__ta'); ta.focus(); ta.select(); document.execCommand('insertText', false, t); }""", long)
    pg.evaluate("document.querySelector('.ce__ta').scrollTop = 600")
    pg.wait_for_timeout(300)
    tops = pg.evaluate("""() => ({ ta: document.querySelector('.ce__ta').scrollTop,
      inner: new DOMMatrix(getComputedStyle(document.querySelector('.ce__inner')).transform).f,
      gut: new DOMMatrix(getComputedStyle(document.querySelector('.ce__gutter-inner')).transform).f })""")
    check("scrolling moves the painted layer with the text", abs(tops["inner"] + tops["ta"]) < 1, tops)
    check("scrolling moves the line numbers with the text", abs(tops["gut"] + tops["ta"]) < 1, tops)
    pg.keyboard.press("Control+Home")
    pg.wait_for_timeout(300)
    check("Ctrl+Home scrolls back to the top", pg.evaluate("document.querySelector('.ce__ta').scrollTop") < 40)
    pg.keyboard.press("Control+End")
    pg.wait_for_timeout(100)
    check("Ctrl+End keeps the caret line in view", pg.evaluate("document.querySelector('.ce__ta').scrollTop") > 1000)

    # ---- check / error marker / outline / save -----------------------------------------------------
    clear_editor()
    pg.keyboard.type("def tool_g(args):\nreturn 1\n\nclass Box:\npass\n\nLIMIT = 3\n")   # no leading spaces: the editor auto-indents
    pg.wait_for_timeout(450)
    names = pg.locator(".tm-outline__name").all_inner_texts()
    check("the outline lists functions, classes and constants", names == ["tool_g", "Box", "LIMIT"], names)
    pg.locator(".tm-outline__item").nth(1).click()
    check("clicking an outline entry moves the caret to that line", pg.evaluate("document.querySelector('.ce__ta').value.slice(0, document.querySelector('.ce__ta').selectionStart).split('\\n').length") == 4)
    state["check_error"] = "line 2: invalid syntax"
    pg.locator("#tm-ed-check").click()
    check("a failed check marks the line in the gutter", settle(lambda: pg.locator(".ce__gutter-inner .is-err").count() == 1))
    check("the error band is shown on that line", pg.locator(".ce__band--err").is_visible())
    check("the problem offers a jump to the line", pg.locator(".tm-jump").count() == 1)
    ta.focus(); pg.keyboard.press("Control+End"); pg.keyboard.type("x")
    check("editing clears the error marker", pg.locator(".ce__gutter-inner .is-err").count() == 0)
    state["check_error"] = None
    state["writes"].clear()
    pg.locator("#tm-ed-name").fill("my_new_tool")
    ta.focus()
    pg.keyboard.press("Control+s")
    check("Ctrl+S saves exactly the editor's text", settle(lambda: len(state["writes"]) == 1) and state["writes"][0] == val(), state["writes"][:1])
    pg.screenshot(path="/tmp/l33_final.png")

    check("no page errors", not errors, errors)
    browser.close()

failed = [n for n, ok in res if not ok]
print("\n%d passed, %d failed" % (len(res) - len(failed), len(failed)))
sys.exit(1 if failed else 0)
