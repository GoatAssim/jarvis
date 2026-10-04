"""L.11 - daemon categories, checked in a real browser against the shipped panel.

Loads web/public/index.html with the real ui-kit.js / category-input.js /
daemons.js / CSS in headless Chromium and answers /api/daemons from a small
stateful fake (PATCH and POST update it the way the CLI would), so no Jarvis
CLI or Node server is needed. Covers: the category filter in the select and in
the Overview, Undefined, the retired "custom" preference, a vanished category,
card chips that are omitted rather than wrapped, and the editor's chip box
(prefix suggestions most-used first, exact spelling reuse, Enter / Tab / comma /
Esc / Backspace, the 8-name cap) through to the PATCH/POST body.

It does NOT exercise web/server.js or the Python registry (test_l11_daemon_
categories.py does) - only the panel's side of the contract.

Needs the `playwright` Python package and a launchable Chromium; prints SKIP
and exits 0 without them.

    python3 tests/verify_l11_daemon_categories.py
"""
import json, mimetypes, sys
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

PUB = Path(__file__).resolve().parent.parent / "web" / "public"
LONG = ["a-rather-long-category-n", "another-quite-long-one-x", "third-long-category-name"]
def fresh():
    return [
        {"id":"scheduler","name":"Scheduler","builtin":True,"status":"running","running":True,"pid":1,"started_at":1,"command":"jarvis sched","categories":["core"]},
        {"id":"discord","name":"Discord","builtin":True,"status":"stopped","command":"jarvis discord","categories":["chat"]},
        {"id":"web","name":"web","builtin":False,"status":"stopped","command":"node s.js","categories":["Utility","chat"]},
        {"id":"bot","name":"bot","builtin":False,"status":"crashed","last_error":"boom","command":"python b.py","categories":["utility"]},
        {"id":"raw","name":"raw","builtin":False,"status":"stopped","command":"python r.py","categories":[]},
        {"id":"legacy","name":"legacy","builtin":False,"status":"stopped","command":"python l.py"},
        {"id":"wide","name":"wide","builtin":False,"status":"stopped","command":"python w.py","categories":LONG},
    ]
DAEMONS = fresh()
sent = []

def handle(route):
    req = route.request
    url = req.url
    path = "/" + (url.split("://",1)[1].split("/",1)[1] if "://" in url else url).split("?")[0]
    ok = lambda body="{}": route.fulfill(status=200, content_type="application/json", body=body)
    if path == "/api/daemons" and req.method == "GET":
        return ok(json.dumps({"daemons": DAEMONS}))
    if path == "/api/daemons" and req.method == "POST":
        body = json.loads(req.post_data); sent.append(("POST", body))
        DAEMONS.append({"id":body["id"],"name":body.get("name",body["id"]),"builtin":False,"status":"stopped","command":body["command"],"categories":body.get("categories",[])})
        return ok('{"ok":true}')
    if path.startswith("/api/daemons/") and req.method == "PATCH":
        did = path.split("/")[3]; body = json.loads(req.post_data); sent.append(("PATCH", did, body))
        for d in DAEMONS:
            if d["id"] == did and "categories" in body: d["categories"] = body["categories"]
        return ok('{"ok":true}')
    if path == "/api/favorite-daemons": return ok("[]")
    if path.startswith("/api/"): return ok()
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
    ctx = b.new_context(viewport={"width":1400,"height":900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: handle(r) if "localhost" in r.request.url else r.abort())

    def boot(prefs=None):
        pg.goto("http://localhost/index.html")
        if prefs is not None:
            pg.evaluate("p=>localStorage.setItem('jarvis.daemons.ui.v1', JSON.stringify(p))", prefs)
            pg.reload()
        pg.wait_for_function("window.JarvisDaemons && window.JarvisCategories")
        pg.evaluate("JarvisDaemons.open()")
        pg.wait_for_selector(".dmn-card")
    names = lambda: pg.eval_on_selector_all(".dmn-card", "els=>els.map(e=>e.dataset.id)")
    options = lambda: pg.eval_on_selector_all("#dmn-kind-filter option", "o=>o.map(x=>[x.value,x.textContent])")
    pick = lambda v: pg.select_option("#dmn-kind-filter", v)

    boot()
    # ---- the select -------------------------------------------------------
    o = options()
    check("select: All, Built-in, then a Categories group", o[0][0] == "all" and o[1][0] == "builtin")
    check("select: no 'Custom' option any more", not any("custom" in (v + t).lower() for v, t in o))
    check("select: one option per category in use, most-used first (ties alphabetical), exact spelling",
          [v for v, _ in o[2:]] == ["cat:chat", "cat:utility", "cat:a-rather-long-category-n", "cat:another-quite-long-one-x", "cat:core", "cat:third-long-category-name", "undefined"]
          and o[3][1] == "Utility (2)")
    check("select: Undefined counts services with no key or an empty list", o[-1] == ["undefined", "Undefined (2)"])
    pick("cat:chat")
    check("filter by a category (a service can be in several)", sorted(names()) == ["discord", "web"])
    pick("cat:utility")
    check("case-insensitive: Utility and utility are one category", sorted(names()) == ["bot", "web"])
    pick("undefined")
    check("Undefined shows the uncategorised", sorted(names()) == ["legacy", "raw"])
    pick("builtin")
    check("Built-in is still its own filter", sorted(names()) == ["discord", "scheduler"])
    pick("all")

    # ---- overview ---------------------------------------------------------
    side = pg.locator("#dmn-side")
    check("overview says 'By category', not 'By kind'", "by category" in side.inner_text().lower() and "by kind" not in side.inner_text().lower())
    side.locator(".dmn-mini__row", has_text="chat").click()
    check("clicking a category row filters the list", sorted(names()) == ["discord", "web"] and pg.input_value("#dmn-kind-filter") == "cat:chat")
    side.locator(".dmn-mini__row", has_text="chat").click()
    check("clicking it again clears the filter", len(names()) == 7 and pg.input_value("#dmn-kind-filter") == "all")

    # ---- cards ------------------------------------------------------------
    chips = lambda i: pg.eval_on_selector_all(f'.dmn-card[data-id="{i}"] .dmn-tag--cat', "e=>e.map(x=>x.textContent)")
    check("a card shows its categories as typed", chips("web") == ["Utility", "chat"])
    check("an uncategorised card shows no chip row", pg.locator('.dmn-card[data-id="raw"] .dmn-card__cats').count() == 0)
    vis = pg.evaluate("""()=>{const c=document.querySelector('.dmn-card[data-id="wide"] .dmn-card__cats');const r=c.getBoundingClientRect();
      const shown=[...c.querySelectorAll('.dmn-tag--cat')].filter(t=>{const a=t.getBoundingClientRect();return a.top>=r.top-1&&a.bottom<=r.bottom+1;}).length;
      return {shown,total:c.children.length,h:r.height};}""")
    check("chips that don't fit are omitted, not wrapped (one row, some hidden)", vis["total"] == 3 and 1 <= vis["shown"] < 3 and vis["h"] <= 18)

    # ---- saved preference migration ----------------------------------------
    boot({"kindFilter": "custom"})
    check("an old saved 'custom' filter lands on All with the full list", pg.input_value("#dmn-kind-filter") == "all" and len(names()) == 7)
    boot({"kindFilter": "cat:vanished"})
    check("a saved category nobody has any more falls back to All (never an unexplained empty list)", pg.input_value("#dmn-kind-filter") == "all" and len(names()) == 7)
    boot({"kindFilter": "cat:chat"})
    check("a saved category that exists is restored", pg.input_value("#dmn-kind-filter") == "cat:chat" and sorted(names()) == ["discord", "web"])
    pick("all")

    # ---- details -----------------------------------------------------------
    pg.locator('.dmn-card[data-id="web"]').click()
    pg.get_by_role("tab", name="Details").click() if pg.get_by_role("tab", name="Details").count() else pg.keyboard.press("2")
    pg.wait_for_selector(".dmn-cats")
    check("Details lists every category", pg.eval_on_selector(".dmn-cats", "e=>[...e.children].map(x=>x.textContent)") == ["Utility", "chat"])
    pg.locator('.dmn-card[data-id="raw"]').click()
    check("Details says Undefined when there are none", "Undefined" in pg.locator("#dmn-main").inner_text())

    # ---- editor: the chip box ------------------------------------------------
    pg.locator('.dmn-card[data-id="raw"]').click()
    pg.keyboard.press("e")
    pg.wait_for_selector("#dmn-f-categories")
    box = pg.locator("#dmn-f-categories")
    sugg = lambda: pg.eval_on_selector_all(".jcat__opt .jcat__optname", "o=>o.map(x=>x.textContent)")
    mine = lambda: pg.eval_on_selector_all(".jcat__chip .jcat__name", "o=>o.map(x=>x.textContent)")
    box.click()
    check("focusing an empty box offers the vocabulary, most-used first", sugg()[:2] == ["chat", "Utility"])
    check("nothing is pre-highlighted on an empty box (Enter must not pick for you)", pg.locator(".jcat__opt.is-hi").count() == 0)
    box.fill("ut")
    check("typing a prefix filters, with the existing spelling", sugg() == ["Utility"])
    check("the best match is pre-highlighted", pg.locator(".jcat__opt.is-hi").count() == 1)
    pg.keyboard.press("Enter")
    check("Enter accepts the highlighted suggestion", mine() == ["Utility"])
    box.fill("CHAT"); pg.keyboard.press("Enter")
    check("typing a name in another case reuses the existing spelling", mine() == ["Utility", "chat"])
    box.fill("brand new"); pg.keyboard.press("Enter")
    check("a name that matches nothing is created by Enter", mine() == ["Utility", "chat", "brand new"])
    box.fill("utility"); pg.keyboard.press("Enter")
    check("a duplicate is ignored quietly", mine() == ["Utility", "chat", "brand new"])
    box.fill("co")
    pg.keyboard.press("Escape")
    check("Esc with the list open closes only the list - the editor stays", pg.locator(".jcat__list").is_hidden() and pg.locator(".dmn-editor").count() == 1)
    pg.keyboard.press("Enter")
    check("after Esc, Enter keeps the text exactly as typed ('co', not 'core')", mine()[-1] == "co")
    box.fill(""); box.press_sequentially("zed,")
    check("a comma commits what was typed", mine()[-1] == "zed")
    box.fill("")
    pg.keyboard.press("Backspace")
    check("Backspace on an empty box removes the last chip", mine() == ["Utility", "chat", "brand new", "co"])
    box.fill("cor"); pg.keyboard.press("Tab")
    check("Tab accepts the highlighted suggestion ('cor' -> core) and stays in the box", mine()[-1] == "core" and pg.evaluate("document.activeElement.id") == "dmn-f-categories")
    pg.locator('.jcat__chip[data-key="co"] .jcat__x').click()
    pg.locator('.jcat__chip[data-key="core"] .jcat__x').click()
    check("the x on a chip removes it", mine() == ["Utility", "chat", "brand new"])
    check("Save is enabled (the form is dirty)", pg.locator('[data-act="save"]').is_enabled())
    pg.locator('[data-act="save"]').click()
    pg.wait_for_function("window.__x||true")
    for _ in range(60):
        if any(s[0] == "PATCH" for s in sent): break
        pg.wait_for_timeout(50)
    patch = [s for s in sent if s[0] == "PATCH"][-1]
    check("save sends the whole set to PATCH /api/daemons/raw", patch[1] == "raw" and patch[2]["categories"] == ["Utility", "chat", "brand new"])
    check("only what changed was sent", set(patch[2]) == {"categories"})
    pg.wait_for_selector(".dmn-card")
    pg.wait_for_function("document.querySelectorAll('#dmn-kind-filter option[value=\"cat:brand new\"]').length===1")
    check("the new category appears in the filter straight away", "cat:brand new" in [v for v, _ in options()])
    check("and the card shows it", chips("raw") == ["Utility", "chat", "brand new"])

    # clear everything -> []
    pg.locator('.dmn-card[data-id="raw"]').click(); pg.keyboard.press("e"); pg.wait_for_selector("#dmn-f-categories")
    for _ in range(3): pg.locator(".jcat__chip .jcat__x").first.click()
    pg.locator('[data-act="save"]').click()
    for _ in range(60):
        if len([s for s in sent if s[0] == "PATCH"]) == 2: break
        pg.wait_for_timeout(50)
    check("removing the last chip sends [] (server -> --clear-categories)", [s for s in sent if s[0] == "PATCH"][-1][2] == {"categories": []})
    pg.wait_for_function("document.querySelector('#dmn-kind-filter option[value=\"cat:brand new\"]')===null")
    check("a category with no services left disappears from the filter", "cat:brand new" not in [v for v, _ in options()])

    # cap
    pg.locator('.dmn-card[data-id="raw"]').click(); pg.keyboard.press("e"); pg.wait_for_selector("#dmn-f-categories")
    for i in range(8):
        pg.locator("#dmn-f-categories").fill(f"cat{i}"); pg.keyboard.press("Enter")
    check("eight is the cap: the box stops taking more", pg.locator("#dmn-f-categories").is_disabled() and len(mine()) == 8)
    check("... and says so", "Up to 8" in (pg.get_attribute("#dmn-f-categories", "placeholder") or ""))
    pg.locator(".jcat__chip .jcat__x").first.click()
    check("removing one re-opens the box", pg.locator("#dmn-f-categories").is_enabled())
    check("a name can't be typed past 24 characters", pg.get_attribute("#dmn-f-categories", "maxlength") == "24")
    pg.locator('[data-act="save"]').click()
    pg.wait_for_timeout(300)

    # ---- a built-in can be re-categorised, and never sends its locked fields ----
    pg.locator('.dmn-card[data-id="discord"]').click(); pg.keyboard.press("e"); pg.wait_for_selector("#dmn-f-categories")
    pg.locator("#dmn-f-categories").fill("gateway"); pg.keyboard.press("Enter")
    pg.locator('[data-act="save"]').click()
    for _ in range(60):
        if sent and sent[-1][0] == "PATCH" and sent[-1][1] == "discord": break
        pg.wait_for_timeout(50)
    check("a built-in's categories are editable, sending nothing it has locked", sent[-1][1] == "discord" and sent[-1][2] == {"categories": ["chat", "gateway"]})

    # ---- add flow ---------------------------------------------------------------
    pg.wait_for_selector(".dmn-editor", state="detached")
    pg.keyboard.press("n"); pg.wait_for_selector("input#dmn-f-id")
    pg.fill("#dmn-f-id", "newsvc"); pg.fill("#dmn-f-command", "python n.py")
    pg.locator("#dmn-f-categories").fill("tools"); pg.keyboard.press("Enter")
    pg.locator('[data-act="save"]').click()
    for _ in range(60):
        if any(s[0] == "POST" for s in sent): break
        pg.wait_for_timeout(50)
    post = [s for s in sent if s[0] == "POST"][-1]
    check("adding a daemon sends its categories", post[1]["id"] == "newsvc" and post[1]["categories"] == ["tools"])

    # ---- empty add sends none -----------------------------------------------------
    pg.wait_for_selector(".dmn-editor", state="detached")
    pg.keyboard.press("n"); pg.wait_for_selector("input#dmn-f-id")
    pg.fill("#dmn-f-id", "plain"); pg.fill("#dmn-f-command", "python p.py")
    pg.locator('[data-act="save"]').click()
    for _ in range(60):
        if len([s for s in sent if s[0] == "POST"]) == 2: break
        pg.wait_for_timeout(50)
    check("adding with no categories sends no 'categories' key", "categories" not in [s for s in sent if s[0] == "POST"][-1][1])

    check("no uncaught page errors", not errs)
    for e in errs[:3]: print("   ", e)
    b.close()
print(f"{sum(res)}/{len(res)} passed")
sys.exit(0 if all(res) else 1)
