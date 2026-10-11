"""L.14 UI rework - the Commands panel's category dropdown, in a real browser.

Loads web/public/index.html with the real app.js / category-input.js /
category-menu.js / CSS in headless Chromium and answers /api/status,
/api/commands and /api/command-categories/{rename,delete} from a small stateful
fake that edits the commands the way server.js does, so no Jarvis CLI or Node
server is needed. Covers: the header no longer overflowing (the old third button
pushed "+ New" off the panel), the dropdown's rows and counts, filtering (and
combining with search), the saved choice and a vanished one, the clear button,
inline rename / merge / delete including every refusal, a server error, the
keyboard, a name that looks like markup, the "Find a category" box, and that
hovering a row never changes its size.

It does NOT exercise web/server.js or the Python registry (test_category_manage.py
does) - only the panel's side of the contract.

Needs the `playwright` Python package and a launchable Chromium; prints SKIP and
exits 0 without them.

    python3 tests/verify_command_category_menu_ui.py
"""
import copy, json, mimetypes, sys
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

PUB = Path(__file__).resolve().parent.parent / "web" / "public"

def spec(desc, cats=None):
    s = {"description": desc, "run": [{"run": "echo hi"}]}
    if cats is not None: s["categories"] = cats
    return s

def fresh():
    return {
        "deploy-site":   spec("Build and push the site", ["Work", "Deploy"]),
        "backup-docs":   spec("Zip Documents to the NAS", ["Backup", "Files"]),
        "game-night":    spec("Open Discord and Steam", ["Gaming", "Media"]),
        "morning-brief": spec("Weather and calendar", ["Daily"]),
        "clean-temp":    spec("Delete old temp files", ["Files", "Maintenance"]),
        "start-music":   spec("Open Spotify", ["Media"]),
        "focus-mode":    spec("Mute notifications", ["Work", "Daily"]),
        "update-all":    spec("Update every package manager", ["Maintenance"]),
        "scratch":       spec("No category at all"),
        "another":       spec("Empty list", []),
    }

COMMANDS = fresh()
sent = []
fail_next = {"on": False}

def key(n): return n.strip().lower()

def handle(route):
    req = route.request
    url = req.url
    path = "/" + (url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url).split("?")[0]
    ok = lambda body="{}": route.fulfill(status=200, content_type="application/json", body=body)
    if path == "/api/status":
        return ok(json.dumps({"online": True, "invocation": "jarvis", "configPath": "~/.jarvis"}))
    if path == "/api/commands" and req.method == "GET":
        return ok(json.dumps(COMMANDS))
    if path == "/api/favorites":
        return ok('{"names":[]}' if req.method == "GET" else "{}")
    if path in ("/api/command-categories/rename", "/api/command-categories/delete") and req.method == "POST":
        body = json.loads(req.post_data); sent.append((path.rsplit("/", 1)[1], body))
        if fail_next["on"]:
            fail_next["on"] = False
            return route.fulfill(status=500, content_type="application/json", body='{"ok":false,"error":"commands.json is read-only"}')
        changed, merged = 0, False
        for s in COMMANDS.values():
            cs = s.get("categories", [])
            if path.endswith("rename"):
                if any(key(c) == key(body["from"]) for c in cs):
                    changed += 1
                    had = any(key(c) == key(body["to"]) and key(body["to"]) != key(body["from"]) for c in cs)
                    merged = merged or any(key(c) == key(body["to"]) for c in sum((x.get("categories", []) for x in COMMANDS.values()), [])) and key(body["to"]) != key(body["from"])
                    out = [c for c in cs if key(c) != key(body["from"])] if had else [body["to"] if key(c) == key(body["from"]) else c for c in cs]
                    s["categories"] = out
            else:
                if any(key(c) == key(body["name"]) for c in cs):
                    changed += 1
                    s["categories"] = [c for c in cs if key(c) != key(body["name"])]
                    if not s["categories"]: del s["categories"]
        return ok(json.dumps({"ok": True, "changed": changed, "merged": merged}))
    if path.startswith("/api/"):
        return ok("[]" if path == "/api/conversations" else "{}")
    f = PUB / path.lstrip("/")
    if path in ("/", ""): f = PUB / "index.html"
    if f.is_file():
        return route.fulfill(status=200, content_type=mimetypes.guess_type(str(f))[0] or "text/plain", body=f.read_bytes())
    return route.fulfill(status=404, body="")

res = []
def check(name, cond):
    res.append(bool(cond)); print(("ok      " if cond else "FAILED  ") + name)

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: handle(r) if "localhost" in r.request.url else r.abort())

    def boot(store=None, width=1400):
        global COMMANDS
        COMMANDS = fresh(); sent.clear(); fail_next["on"] = False
        pg.set_viewport_size({"width": width, "height": 900})
        pg.goto("http://localhost/index.html")
        pg.evaluate("s=>{localStorage.clear(); if(s) localStorage.setItem('jarvis.commands.category.v1', s)}", store)
        pg.reload()
        pg.wait_for_selector("#cmd-list .cmd-card", timeout=15000)
        pg.wait_for_function("document.querySelector('.cmenu__trigger')")
    def wait_until(fn, seconds=4.0):
        import time
        end = time.time() + seconds
        while time.time() < end:
            if fn(): return True
            pg.wait_for_timeout(40)
        return bool(fn())
    cards = lambda: sorted(pg.eval_on_selector_all("#cmd-list .cmd-card .cmd-card__name", "e=>e.map(x=>x.textContent)"))
    label = lambda: pg.inner_text(".cmenu__label")
    rows = lambda: pg.eval_on_selector_all(".cmenu__pick", "e=>e.map(x=>[x.querySelector('.cmenu__name').textContent, +x.querySelector('.cmenu__n').textContent])")
    row = lambda name: pg.locator(".cmenu__item", has=pg.locator(".cmenu__name", has_text=name)).first
    open_menu = lambda: (pg.click(".cmenu__trigger") if pg.locator(".cmenu__menu").is_hidden() else None, pg.wait_for_selector(".cmenu__menu:not([hidden])"))
    def hover_act(name, act):
        row(name).hover()
        pg.locator(".cmenu__item", has=pg.locator(".cmenu__name", has_text=name)).first.locator(f'[data-act="{act}"]').click()

    # ---- header -----------------------------------------------------------------
    boot()
    check("the old Categories button is gone", pg.locator("#btn-command-categories").count() == 0)
    geo = pg.evaluate("""()=>{const pn=document.querySelector('.panel--list').getBoundingClientRect();const h=document.querySelector('.panel--list .panel__head');
      return {noOverflow:h.scrollWidth<=h.clientWidth, btns:[...h.querySelectorAll('.panel__head-actions .btn')].map(x=>{const r=x.getBoundingClientRect();return r.left>=pn.left&&r.right<=pn.right&&r.width>0})}}""")
    check("Config and + New both sit inside the panel (the header no longer overflows)", geo["noOverflow"] and geo["btns"] == [True, True])
    boot(width=1100)
    geo = pg.evaluate("""()=>{const pn=document.querySelector('.panel--list').getBoundingClientRect();const h=document.querySelector('.panel--list .panel__head');
      return {noOverflow:h.scrollWidth<=h.clientWidth, btns:[...h.querySelectorAll('.panel__head-actions .btn')].map(x=>{const r=x.getBoundingClientRect();return r.left>=pn.left&&r.right<=pn.right&&r.width>0}), narrow:pn.width}}""")
    check("...and in the narrower 260px layout too (wraps rather than clips)", geo["narrow"] < 280 and geo["noOverflow"] and geo["btns"] == [True, True])
    boot()

    # ---- the closed trigger -------------------------------------------------------
    check("trigger starts on All categories with the total", label() == "All categories" and pg.inner_text(".cmenu__count") == "10")
    check("the clear button is hidden while nothing is filtered", pg.locator(".cmenu__clear").is_hidden())
    check("the menu is closed to begin with", pg.locator(".cmenu__menu").is_hidden() and pg.get_attribute(".cmenu__trigger", "aria-expanded") == "false")

    # ---- the menu ----------------------------------------------------------------
    open_menu()
    check("trigger reports open", pg.get_attribute(".cmenu__trigger", "aria-expanded") == "true")
    check("rows: All, Uncategorized, then categories most-used first (ties alphabetical)",
          rows() == [["All commands", 10], ["Uncategorized", 2], ["Daily", 2], ["Files", 2], ["Maintenance", 2], ["Media", 2], ["Work", 2], ["Backup", 1], ["Deploy", 1], ["Gaming", 1]])
    check("the active row is marked", pg.get_attribute(".cmenu__pick.is-active", "data-value") == "all")
    check("focus lands on the active row", pg.evaluate("document.activeElement.classList.contains('cmenu__pick') && document.activeElement.dataset.value") == "all")
    check("no 'Find a category' box for a short list", pg.locator(".cmenu__find").count() == 0)

    # ---- filtering -----------------------------------------------------------------
    pg.click('.cmenu__pick[data-value="cat:work"]')
    check("picking a category filters the list", cards() == ["deploy-site", "focus-mode"])
    check("the menu closes and the trigger names the filter", pg.locator(".cmenu__menu").is_hidden() and label() == "Work" and pg.inner_text(".cmenu__count") == "2")
    check("focus returns to the trigger", pg.evaluate("document.activeElement.classList.contains('cmenu__trigger')"))
    check("a filtered trigger shows the clear button", pg.locator(".cmenu__clear").is_visible())
    pg.fill("#cmd-search", "deploy")
    check("search and category combine", cards() == ["deploy-site"])
    pg.fill("#cmd-search", "scratch")
    empty = pg.inner_text("#cmd-list")
    check("an empty result says which filter caused it and offers a way out", "scratch" in empty and "Work" in empty and pg.get_by_role("button", name="Show all commands").count() >= 1)
    pg.locator("#cmd-list").get_by_role("button", name="Show all commands").click()
    check("that button clears the filter (search text stays)", label() == "All categories" and cards() == ["scratch"])
    pg.fill("#cmd-search", "")
    check("clearing search brings everything back", len(cards()) == 10)

    # ---- saved choice ----------------------------------------------------------------
    open_menu(); pg.click('.cmenu__pick[data-value="cat:media"]')
    pg.reload(); pg.wait_for_selector("#cmd-list .cmd-card")
    check("the choice survives a reload", label() == "Media" and cards() == ["game-night", "start-music"])
    boot("cat:vanished")
    check("a saved category nobody has any more falls back to All (never an unexplained empty list)", label() == "All categories" and len(cards()) == 10)
    boot("garbage")
    check("a junk saved value is ignored", label() == "All categories")

    # ---- uncategorized + clear button -------------------------------------------------
    open_menu(); pg.click('.cmenu__pick[data-value="none"]')
    check("Uncategorized shows commands with no key or an empty list", cards() == ["another", "scratch"] and label() == "Uncategorized")
    pg.click(".cmenu__clear")
    check("the x button shows everything again", label() == "All categories" and len(cards()) == 10 and pg.locator(".cmenu__clear").is_hidden())

    # ---- layout stability ----------------------------------------------------------------
    open_menu()
    before = pg.evaluate("()=>{const i=[...document.querySelectorAll('.cmenu__item')].find(x=>x.textContent.includes('Files'));const r=i.getBoundingClientRect();const n=i.querySelector('.cmenu__n').getBoundingClientRect();return [r.height,r.width,n.left]}")
    row("Files").hover()
    after = pg.evaluate("()=>{const i=[...document.querySelectorAll('.cmenu__item')].find(x=>x.textContent.includes('Files'));const r=i.getBoundingClientRect();const n=i.querySelector('.cmenu__n').getBoundingClientRect();return [r.height,r.width,n.left]}")
    check("hovering a row changes neither its size nor where the count sits", before == after)
    pg.wait_for_timeout(300)  # the icons fade in over 120 ms
    vis = pg.evaluate("getComputedStyle(document.querySelector('.cmenu__item:hover .cmenu__acts')).opacity")
    check("the rename / delete icons appear on hover", float(vis) > 0.9)

    # ---- rename --------------------------------------------------------------------------
    hover_act("Files", "rename")
    check("rename opens in the row, prefilled and focused", pg.input_value(".cmenu__input") == "Files" and pg.evaluate("document.activeElement.classList.contains('cmenu__input')"))
    pg.fill(".cmenu__input", "  ")
    check("a blank name is refused up front", "name" in pg.inner_text('[data-edit="rename"] [data-note]').lower())
    pg.press(".cmenu__input", "Enter")
    check("...and nothing is sent", sent == [])
    pg.fill(".cmenu__input", "x" * 30)
    pg.press(".cmenu__input", "Enter")
    check("a too-long name is refused with the limit, nothing sent", "24" in pg.inner_text('[data-edit="rename"] [data-note]') and sent == [])
    pg.fill(".cmenu__input", "Files")
    pg.press(".cmenu__input", "Enter")
    check("renaming to the same name just closes the editor", sent == [] and pg.locator(".cmenu__input").count() == 0)
    hover_act("Files", "rename")
    pg.fill(".cmenu__input", "Documents")
    check("a fresh name shows the plain hint and a Rename button", pg.inner_text(".cmenu__save").lower() == "rename")
    pg.press(".cmenu__input", "Enter")
    pg.wait_for_function("document.querySelector('.cmenu__menu .cmenu__name') && [...document.querySelectorAll('.cmenu__name')].some(x=>x.textContent==='Documents')")
    check("rename posts {from, to}", sent == [("rename", {"from": "Files", "to": "Documents"})])
    check("the commands carry the new name everywhere", COMMANDS["backup-docs"]["categories"] == ["Backup", "Documents"] and COMMANDS["clean-temp"]["categories"] == ["Documents", "Maintenance"])
    check("the menu stays open and shows it", pg.locator(".cmenu__menu").is_visible() and ["Documents", 2] in rows())
    check("the card chips follow", "Documents" in pg.inner_text('#cmd-list .cmd-card:has-text("backup-docs")'))

    # a selected category follows its rename
    pg.click('.cmenu__pick[data-value="cat:documents"]')
    open_menu(); hover_act("Documents", "rename"); pg.fill(".cmenu__input", "Docs"); pg.press(".cmenu__input", "Enter")
    pg.wait_for_function("[...document.querySelectorAll('.cmenu__name')].some(x=>x.textContent==='Docs')")
    check("a filter on the renamed category follows the new name", label() == "Docs" and cards() == ["backup-docs", "clean-temp"])

    # ---- merge ---------------------------------------------------------------------------
    pg.click(".cmenu__clear"); open_menu()
    hover_act("Media", "rename")
    pg.fill(".cmenu__input", "gaming")
    note = pg.inner_text('[data-edit="rename"] [data-note]')
    check("typing a name in use says it will merge, with the counts", "Merges with" in note and "Gaming" in note and "1" in note)
    check("the button becomes Merge", pg.inner_text(".cmenu__save").lower() == "merge")
    pg.press(".cmenu__input", "Enter")
    wait_until(lambda: sent and sent[-1][0] == "rename" and "gaming" in str(sent[-1]))
    pg.wait_for_function("![...document.querySelectorAll('.cmenu__name')].some(x=>x.textContent==='Media') && !document.querySelector('.cmenu__input')")
    check("merge posts the typed name", sent[-1] == ("rename", {"from": "Media", "to": "gaming"}))
    check("the two become one category, keeping the existing spelling", COMMANDS["game-night"]["categories"] == ["Gaming"] and COMMANDS["start-music"]["categories"] == ["gaming"])
    merged_rows = [r for r in rows() if r[0].lower() == "gaming"]
    check("the merged row counts both", len(merged_rows) == 1 and merged_rows[0][1] == 2)

    # ---- server error ---------------------------------------------------------------------
    hover_act("Work", "rename"); pg.fill(".cmenu__input", "Job")
    fail_next["on"] = True
    pg.press(".cmenu__input", "Enter")
    pg.wait_for_function("document.querySelector('[data-edit=\"rename\"] .cmenu__note.is-error')")
    check("a server refusal is shown in the row and the editor stays open", "read-only" in pg.inner_text(".cmenu__note.is-error") and pg.input_value(".cmenu__input") == "Job")
    check("...and nothing changed", COMMANDS["deploy-site"]["categories"] == ["Work", "Deploy"])
    pg.press(".cmenu__input", "Escape")
    check("Escape closes just the editor, not the menu", pg.locator(".cmenu__input").count() == 0 and pg.locator(".cmenu__menu").is_visible())

    # ---- delete ---------------------------------------------------------------------------
    pg.click('.cmenu__pick[data-value="cat:work"]')
    open_menu(); hover_act("Work", "delete")
    ask = pg.inner_text(".cmenu__ask")
    check("delete asks first, naming the category and how many commands", "Work" in ask and "2 commands" in ask and "stay" in ask)
    check("the safe answer (Keep) has focus", pg.evaluate("document.activeElement.classList.contains('cmenu__keep')"))
    pg.click(".cmenu__keep")
    check("Keep cancels with nothing sent", pg.locator(".cmenu__ask").count() == 0 and not any(s[0] == "delete" for s in sent))
    hover_act("Work", "delete"); pg.click(".cmenu__del")
    wait_until(lambda: sent and sent[-1][0] == "delete")
    pg.wait_for_function("!document.querySelector('.cmenu__ask') && ![...document.querySelectorAll('.cmenu__name')].some(x=>x.textContent==='Work')")
    check("delete posts {name}", sent[-1] == ("delete", {"name": "Work"}))
    check("the commands are untouched, only the label is gone",
          len(COMMANDS) == 10 and COMMANDS["deploy-site"]["categories"] == ["Deploy"] and COMMANDS["focus-mode"]["categories"] == ["Daily"])
    check("a filter on the deleted category falls back to All", label() == "All categories" and len(cards()) == 10)
    pg.keyboard.press("Escape")

    # ---- keyboard + closing ------------------------------------------------------------------
    pg.focus(".cmenu__trigger"); pg.keyboard.press("ArrowDown")
    check("ArrowDown on the trigger opens the menu", pg.locator(".cmenu__menu").is_visible())
    pg.keyboard.press("ArrowDown"); pg.keyboard.press("ArrowDown")
    check("arrows move between rows", pg.evaluate("document.activeElement.dataset.value") not in ("all", None))
    pg.keyboard.press("Home")
    check("Home goes to the first row", pg.evaluate("document.activeElement.dataset.value") == "all")
    pg.keyboard.press("End")
    check("End goes to the last", pg.evaluate("document.activeElement.classList.contains('cmenu__pick')"))
    pg.keyboard.press("Escape")
    check("Escape closes the menu and returns to the trigger", pg.locator(".cmenu__menu").is_hidden() and pg.evaluate("document.activeElement.classList.contains('cmenu__trigger')"))
    open_menu(); pg.click("#cmd-search")
    check("clicking elsewhere closes it", pg.locator(".cmenu__menu").is_hidden())
    open_menu(); pg.click(".cmenu__trigger")
    check("clicking the trigger again closes it", pg.locator(".cmenu__menu").is_hidden())

    # ---- a name that looks like markup -------------------------------------------------------------
    boot()
    COMMANDS["scratch"]["categories"] = ['<img src=x onerror="window.__x=1">']
    pg.evaluate("document.querySelector('#cmd-search').dispatchEvent(new Event('input',{bubbles:true}))")
    pg.fill("#cmd-search", ""); pg.evaluate("fetch('/api/commands').then(r=>r.json())")
    pg.reload(); pg.wait_for_selector("#cmd-list .cmd-card")
    open_menu()
    check("a markup-looking name is text, never an element", pg.locator(".cmenu__menu img").count() == 0 and pg.evaluate("window.__x") is None)

    # ---- many categories: the find box --------------------------------------------------------------
    boot()
    for i, nm in enumerate(["Alpha", "Beta", "Gamma", "Delta"]):
        COMMANDS[list(COMMANDS)[i]]["categories"] = COMMANDS[list(COMMANDS)[i]].get("categories", []) + [nm]
    pg.reload(); pg.wait_for_selector("#cmd-list .cmd-card")
    open_menu()
    check("a long list gets a Find box", pg.locator(".cmenu__find").count() == 1)
    pg.fill(".cmenu__find", "ga")
    names = [r[0] for r in rows()]
    check("typing narrows to matching categories (no All / Uncategorized rows while searching)", set(names) == {"Gamma", "Gaming"} and len(names) == 2)
    check("the box keeps focus while narrowing", pg.evaluate("document.activeElement.classList.contains('cmenu__find')"))
    pg.fill(".cmenu__find", "zzz")
    check("no match says so", "No category matches" in pg.inner_text(".cmenu__menu"))

    # ---- nothing to manage -----------------------------------------------------------------------------
    boot()
    for s in COMMANDS.values(): s.pop("categories", None)
    pg.reload(); pg.wait_for_selector("#cmd-list .cmd-card")
    check("with no categories the trigger says so", label() == "No categories yet")
    open_menu()
    check("the menu explains how to add one instead of showing nothing", "Edit" in pg.inner_text(".cmenu__menu") and "Categories" in pg.inner_text(".cmenu__menu"))

    # ---- short and narrow windows: the panel clips what spills out of it ---------------------------------
    inside = """()=>{const pn=document.querySelector('.panel--list').getBoundingClientRect();const m=document.querySelector('.cmenu__menu');const r=m.getBoundingClientRect();
      return {inside: r.top>=pn.top-1 && r.bottom<=pn.bottom+1, up: m.classList.contains('cmenu__menu--up'), h: r.height}}"""
    boot(width=1100)   # list is 260px wide and the panel is only ~350px tall
    for height in (900, 800, 700):
        pg.set_viewport_size({"width": 1100, "height": height}); pg.wait_for_timeout(150)
        pg.keyboard.press("Escape") if pg.locator(".cmenu__menu").is_visible() else None
        open_menu(); g = pg.evaluate(inside)
        check(f"a short panel ({height}px window): the menu stays inside it, opening upward if there is no room below", g["inside"] and g["h"] >= 90)
        pg.keyboard.press("Escape")
    open_menu(); pg.keyboard.press("End")
    check("...and its list still scrolls to the last row", pg.evaluate("document.activeElement.classList.contains('cmenu__pick')"))
    pg.keyboard.press("Escape")
    boot(width=1400); pg.set_viewport_size({"width": 1400, "height": 520}); pg.wait_for_timeout(150)
    open_menu(); g = pg.evaluate(inside)
    check("a short window at full width: still inside the panel", g["inside"])
    pg.set_viewport_size({"width": 1400, "height": 900}); pg.wait_for_timeout(150)
    boot(width=600)    # stacked phone layout: the menu opens in the flow
    open_menu()
    pos = pg.evaluate("getComputedStyle(document.querySelector('.cmenu__menu')).position")
    g = pg.evaluate(inside)
    check("stacked layout: the menu opens in the flow and is fully visible", pos == "static" and g["inside"])
    check("...with no sideways scroll", not pg.evaluate("document.documentElement.scrollWidth > innerWidth"))

    check("no page errors", errs == [])
    if errs: print(errs)
    b.close()

print(f"\n{sum(res)}/{len(res)} checks passed")
sys.exit(0 if all(res) else 1)
