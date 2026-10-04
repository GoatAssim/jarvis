"""L.13 - favorite daemons, checked in a real browser against the shipped panel.

Loads web/public/index.html with the real ui-kit.js / daemons.js / CSS in
headless Chromium and answers /api/daemons and /api/favorite-daemons from an
in-memory fake, so no Jarvis CLI or Node server is needed. Covers: the saved
star showing, floating to the top, starring from a card without changing the
selection, the Favorites chip composing with state/search filters, the `*` key,
persistence across a reload, the "no favorites yet" empty state, and the
roll-back when a save fails.

It does NOT exercise web/server.js (the routes themselves need express, which
is not part of this test) - only the panel's side of the contract.

Needs the `playwright` Python package and a launchable Chromium; prints SKIP
and exits 0 without them, like verify_l9_sequence_bar.js.

    python3 tests/verify_l13_favorite_daemons.py
"""
import json, mimetypes, sys
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

PUB = Path(__file__).resolve().parent.parent / "web" / "public"
DAEMONS = [
    {"id":"restart_jarvis_server","name":"Restart server","builtin":True,"status":"running","running":True,"pid":11,"started_at":1,"command":"node a"},
    {"id":"tts_bot","name":"TTS bot","builtin":True,"status":"stopped","command":"python tts.py"},
    {"id":"spotify","name":"spotify","builtin":False,"status":"stopped","command":"python s.py"},
    {"id":"worker","name":"worker","builtin":False,"status":"crashed","last_error":"boom","command":"python w.py"},
    {"id":"cron","name":"cron","builtin":False,"status":"stopped","command":"python c.py"},
]
server_favs = ["spotify"]
posts = []
fail_next_post = {"on": False}

def handle(route):
    global server_favs
    req = route.request
    url = req.url
    path = url.split("://",1)[1].split("/",1)[1] if "://" in url else url
    path = "/" + path.split("?")[0]
    if path == "/api/favorite-daemons":
        if req.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(server_favs))
        if fail_next_post["on"]:
            return route.fulfill(status=500, content_type="application/json", body=json.dumps({"error":"disk full"}))
        ids = json.loads(req.post_data)["ids"]; posts.append(ids); server_favs = ids
        return route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
    if path == "/api/daemons":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"daemons":DAEMONS}))
    if path.startswith("/api/"):
        return route.fulfill(status=200, content_type="application/json", body="{}")
    f = PUB / path.lstrip("/")
    if path in ("/", ""): f = PUB / "index.html"
    if f.is_file():
        return route.fulfill(status=200, content_type=mimetypes.guess_type(str(f))[0] or "text/plain", body=f.read_bytes())
    return route.fulfill(status=404, body="")

res = []
def settle(pred, ms=3000):
    """The UI updates optimistically and saves a moment later; wait for the
    fake server to have received the save before asserting on it."""
    for _ in range(ms // 50):
        if pred():
            return True
        pg.wait_for_timeout(50)
    return pred()

def check(name, cond):
    res.append(bool(cond)); print(("ok      " if cond else "FAILED  ") + name)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width":1400,"height":900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: handle(r) if "localhost" in r.request.url else r.abort())
    pg.goto("http://localhost/index.html")
    pg.wait_for_function("window.JarvisDaemons")
    pg.evaluate("JarvisDaemons.open()")
    pg.wait_for_selector(".dmn-card")
    pg.wait_for_function("document.querySelectorAll('.dmn-star.is-on').length===1")

    names = lambda: pg.eval_on_selector_all(".dmn-card", "els=>els.map(e=>e.dataset.id)")
    check("saved favorite shows as a solid star", pg.eval_on_selector_all(".dmn-star.is-on", "e=>e.map(x=>x.closest('.dmn-card').dataset.id)") == ["spotify"])
    check("favorite floats to the top of its group (custom: spotify first)", names() == ["restart_jarvis_server","tts_bot","spotify","worker","cron"])
    chip = pg.locator(".dmn-chip--fav")
    check("Favorites chip count is 1", chip.locator(".dmn-chip__n").inner_text() == "1")

    # star another via card click: must not select it
    sel_before = pg.eval_on_selector(".dmn-card.is-active", "e=>e.dataset.id")
    pg.locator('.dmn-card[data-id="cron"] .dmn-star').click()
    pg.wait_for_function("document.querySelectorAll('.dmn-star.is-on').length===2")
    check("starring from a card does not change the selection", pg.eval_on_selector(".dmn-card.is-active", "e=>e.dataset.id") == sel_before)
    check("custom group order: favorites first (cron, spotify keep relative order)", names() == ["restart_jarvis_server","tts_bot","spotify","cron","worker"])
    check("server was sent the full list", settle(lambda: posts and set(posts[-1]) == {"spotify","cron"}))

    # filter
    chip.click()
    check("Favorites filter shows only starred", names() == ["spotify","cron"])
    check("chip is pressed", chip.get_attribute("aria-pressed") == "true")
    # compose with state chip
    pg.locator('.dmn-chip[data-state="stopped"]').click()
    check("Favorites AND Stopped composes", names() == ["spotify","cron"])
    pg.locator('.dmn-chip[data-state="crashed"]').click()
    check("Favorites AND Crashed -> empty message", pg.locator(".dmn-empty-list").count() == 1 and "No favorites yet" not in pg.locator(".dmn-empty-list").inner_text())
    pg.locator('.dmn-chip[data-state="crashed"]').click()  # untoggle
    # search compose
    pg.fill("#dmn-search", "cro")
    check("Favorites AND search composes", names() == ["cron"])
    pg.fill("#dmn-search", "")

    # keyboard: * toggles selected
    pg.locator('.dmn-card[data-id="spotify"]').click()
    pg.keyboard.press("*")
    pg.wait_for_function("document.querySelectorAll('.dmn-star.is-on').length===1")
    check("* key unstars the selected service", settle(lambda: server_favs == ["cron"]))
    # detail button
    btn = pg.locator('.dmn-act[data-act="favorite"]')
    check("detail pane Favorite button present", btn.count() == 1)

    # persistence across reload (server holds the ids)
    pg.keyboard.press("Escape")
    settle(lambda: server_favs == ["cron"])
    pg.reload(); pg.wait_for_function("window.JarvisDaemons"); pg.evaluate("JarvisDaemons.open()")
    pg.wait_for_selector(".dmn-card")
    pg.wait_for_function("document.querySelectorAll('.dmn-star.is-on').length===1")
    check("favorite persists across reload", pg.eval_on_selector_all(".dmn-star.is-on", "e=>e.map(x=>x.closest('.dmn-card').dataset.id)") == ["cron"])
    check("filter-on preference persists across reload", pg.locator(".dmn-chip--fav").get_attribute("aria-pressed") == "true" and names() == ["cron"])

    # empty-state: unstar everything while the filter is on
    pg.locator('.dmn-card[data-id="cron"] .dmn-star').click()
    pg.wait_for_selector(".dmn-empty-list")
    check("zero favorites + filter on -> explanatory empty state", "No favorites yet" in pg.locator(".dmn-empty-list").inner_text())
    pg.locator(".dmn-empty-list button").click()
    check("Clear filters turns the Favorites filter off", pg.locator(".dmn-chip--fav").get_attribute("aria-pressed") == "false" and len(names()) == 5)

    # failure: save fails -> toast + rollback to server truth
    fail_next_post["on"] = True
    pg.locator('.dmn-card[data-id="worker"] .dmn-star').click()
    pg.wait_for_timeout(600)
    check("failed save rolls back to the server's list", pg.locator(".dmn-star.is-on").count() == 0)
    fail_next_post["on"] = False

    pg.screenshot(path="/tmp/l13_favorites.png", clip={"x":0,"y":0,"width":700,"height":520})
    check("no uncaught page errors", not errs)
    if errs: print(errs)
    b.close()
print(f"{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
