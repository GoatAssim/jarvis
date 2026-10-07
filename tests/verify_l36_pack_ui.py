"""L.36-P8/P9/P13/P14/P16 - the last pack items and the add-person panel, in a real browser.

Loads web/public/index.html with the shipped channels-panel.js / channels.css in
headless Chromium. The API is answered from REAL backend output
(tests/_channels_pack_fixture.py: a throwaway HOME, the same functions the CLI
calls). web/server.js is NOT exercised (it needs express); only the panel's side
of the contract -- which requests it makes, with which bodies, and what it draws
from what comes back.

Covers
  P8   the Pictures row (off, owner locked, the "not built yet" note), its POST,
       the Test tab's picture card
  P9   the platform tools switch: first click PREVIEWS and writes nothing; only
       "Turn it on" sends confirm:true; Cancel sends nothing
  P13  the message box: Preview first, "Send it" is the only delivering click and
       sends confirm:true; the text is shown as text; hidden for the owner
  P14  turned-away list: handle / count / reason, NEVER what they wrote; Add posts
       the ordinary add-person route and promises no access
  P16  the add-a-person panel opens, explains a bad id without posting, and posts exactly one add

Needs `playwright` (Python) + a launchable Chromium; prints SKIP and exits 0
without them.

    python3 tests/verify_l36_pack_ui.py
"""
import json, mimetypes, subprocess, sys
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

HERE = Path(__file__).resolve().parent
PUB = HERE.parent / "web" / "public"
fx = json.loads(subprocess.run([sys.executable, str(HERE / "_channels_pack_fixture.py")], capture_output=True, text=True, check=True).stdout)
OWNER, FRIEND, STRANGER = fx["ids"]["owner"], fx["ids"]["friend"], fx["ids"]["stranger"]

posts = []          # (path, body) for every non-GET /api/ call


def handle(route):
    req = route.request
    url = req.url
    path = "/" + (url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url)
    full, path = path, path.split("?")[0]
    j = lambda body, status=200: route.fulfill(status=status, content_type="application/json", body=json.dumps(body))  # noqa: E731
    if req.method != "GET" and path.startswith("/api/"):
        posts.append((path, json.loads(req.post_data or "{}")))
    if path == "/api/channels/people" and req.method == "GET":
        return j(fx["people"])
    if path == "/api/channels/people" and req.method == "POST":
        return j({"ok": True, "user_id": json.loads(req.post_data or "{}").get("ident", ""), "existing": False, "note": "Added."})
    if path == "/api/channels":
        return j({"ok": True, "config": {}})
    if path == "/api/channels/denied":
        return j(fx["denied"])
    if path == "/api/channels/servers":
        return j({"ok": False, "error": "no servers in this fixture"}, 400)
    if path.endswith("/master-tools") and req.method == "POST":
        b = json.loads(req.post_data or "{}")
        if b.get("value") and not b.get("confirm"):
            return j({"ok": True, "dry_run": True, "needs_confirm": True, "applied": False, "view": fx["master"]})
        return j({"ok": True, "applied": True, "note": "tools can now run from this platform"})
    if path.endswith("/dm") and req.method == "POST":
        b = json.loads(req.post_data or "{}")
        if b.get("confirm"):
            return j({"ok": True, "sent": True, "to": fx["dm_preview"]["to"], "chars": len(b["text"])})
        return j(dict(fx["dm_preview"], message=b["text"], chars=len(b["text"])))
    if path.endswith("/flag") and req.method == "POST":
        return j({"ok": True, "note": "recorded"})
    if path.endswith("/test"):
        return j(fx["test"][f"{FRIEND}|dm"])
    if path == "/api/tools":
        return j([])
    if path.startswith("/api/"):
        return j({})
    f = PUB / path.lstrip("/")
    if path in ("/", ""):
        f = PUB / "index.html"
    if f.is_file():
        return route.fulfill(status=200, content_type=mimetypes.guess_type(str(f))[0] or "text/plain", body=f.read_bytes())
    return route.fulfill(status=404, body="")


res = []
def check(name, cond):
    res.append(bool(cond)); print(("ok      " if cond else "FAILED  ") + name)

def settle(pred, ms=3000):
    for _ in range(ms // 50):
        if pred(): return True
        pg.wait_for_timeout(50)
    return pred()

def txt(sel):
    return pg.locator(sel).inner_text().lower()

def mine(suffix):
    return [b for p, b in posts if p.endswith(suffix)]

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: handle(r) if "localhost" in r.request.url else r.abort())
    pg.goto("http://localhost/index.html")
    pg.wait_for_function("window.JarvisChannels")
    pg.evaluate("JarvisChannels.open()")
    pg.wait_for_selector(".ch-card")

    # ---- P14: turned away
    pg.wait_for_selector("#ch-denied .ch-denied__row")
    row = pg.locator("#ch-denied .ch-denied__row").first
    t = row.inner_text().lower()
    check("P14: the stranger is listed by handle with a count and the gate's reason", "rando" in t and "2 messages" in t and "reply_allowlist" in t)
    check("P14: what they wrote is nowhere on the page", fx["secret"].lower() not in pg.locator("body").inner_text().lower())
    check("P14: it says adding gives no access", "still get no replies" in txt("#ch-denied"))
    row.locator(".btn", has_text="Add").click()
    check("P14: Add posts the ordinary add-person route with the id only", settle(lambda: len(mine("/api/channels/people")) == 1)
          and mine("/api/channels/people")[0] == {"platform": "discord", "ident": STRANGER})
    check("P14: ...and nothing that grants access", not any(pth.endswith(("/flag", "/allow")) for pth, _ in posts))

    # ---- P9: master switch
    pg.wait_for_selector("#ch-master-on-discord")
    check("P9: the switch reads off with a Turn on button", pg.locator("#ch-master-off-discord").count() == 0)
    pg.locator("#ch-master-on-discord").click()
    pg.wait_for_selector("#ch-master-confirm-discord")
    check("P9: the first click wrote nothing (a preview)", mine("/master-tools") == [{"value": True, "confirm": False}])
    check("P9: the preview says who it would reach", "1 person" in txt("[data-plat='discord'] .ch-master") or "1 person" in txt("#ch-side"))
    pg.locator("[data-plat='discord'] .ch-master .btn", has_text="Cancel").click()
    check("P9: Cancel sends nothing more", len(mine("/master-tools")) == 1)
    pg.locator("#ch-master-on-discord").click()
    pg.wait_for_selector("#ch-master-confirm-discord")
    pg.locator("#ch-master-confirm-discord").click()
    check("P9: only 'Turn it on' sends confirm:true", settle(lambda: {"value": True, "confirm": True} in mine("/master-tools")))

    # ---- P8: pictures row
    pg.locator(".ch-card", has_text="Friend").first.click()
    pg.wait_for_selector(".ch-hero__name")
    pg.locator("#ch-tab-perms").click()
    pg.wait_for_selector("[data-flag='image']")
    img = pg.locator("[data-flag='image']")
    check("P8: Pictures is a row, off by default", img.count() == 1 and img.locator(".ch-switch").get_attribute("aria-checked") == "false")
    check("P8: it says it changes nothing yet", "doesn't read pictures" in img.inner_text().lower())
    img.locator(".ch-switch").click()
    check("P8: switching it on posts the image flag", settle(lambda: {"flag": "image", "value": True} in mine("/flag")))
    pg.locator(".ch-card", has_text="boss").first.click()
    pg.wait_for_selector("[data-flag='image']")
    check("P8: the owner's row is locked, not a switch", pg.locator("[data-flag='image'] .ch-switch").count() == 0 and "always read" in txt("[data-flag='image']"))

    # ---- P13: message box (Friend: DMs allowed, not the owner)
    pg.locator(".ch-card", has_text="Friend").first.click()
    pg.locator("#ch-tab-profile").click()
    area = pg.locator(".ch-dm__text")
    area.wait_for()
    check("P13: the message box exists for a person Jarvis may DM", area.count() == 1 and area.is_enabled())
    check("P13: Preview is disabled until there is text", pg.locator("#ch-dm-preview").is_disabled())
    area.fill("Hello from the panel <b>x</b>")
    pg.locator("#ch-dm-preview").click()
    pg.wait_for_selector("#ch-dm-send")
    check("P13: Preview sent confirm:false and nothing was delivered", mine("/dm") == [{"text": "Hello from the panel <b>x</b>", "confirm": False}])
    check("P13: it shows the allowance, shared with the tool", "shared with the send_dm tool" in txt(".ch-dm"))
    check("P13: the message is text, not markup", pg.locator(".ch-dm__quote b").count() == 0 and "<b>x</b>" in pg.locator(".ch-dm__quote").inner_text())
    pg.locator("#ch-dm-send").click()
    check("P13: only 'Send it' sends confirm:true", settle(lambda: len(mine("/dm")) == 2 and mine("/dm")[1]["confirm"] is True))
    check("P13: it confirms and clears the box", settle(lambda: "sent." in txt(".ch-dm")) and area.input_value() == "")
    pg.locator(".ch-card", has_text="boss").first.click()
    pg.locator("#ch-tab-profile").click()
    pg.wait_for_selector("#ch-pane-profile")
    check("P13: no message box on the owner's own profile", pg.locator(".ch-dm__text").count() == 0)

    # ---- P8: Test tab picture card
    pg.locator(".ch-card", has_text="Friend").first.click()
    pg.locator("#ch-tab-test").click()
    pg.wait_for_selector(".ch-verdict")
    check("P8: the Test tab has a Pictures card", settle(lambda: "pictures" in txt("#ch-pane-test")))

    # ---- P16: add-a-person panel
    pg.locator("#btn-ch-add").click()
    pg.wait_for_selector("#ch-add-ident")
    pg.locator("#ch-add-ident").fill("not a valid id!!")
    check("P16: a bad id is flagged as you type", pg.locator("#ch-add-ident").get_attribute("aria-invalid") == "true")
    before = len(mine("/api/channels/people"))
    pg.locator("#ch-add-submit").click()
    check("P16: submitting a bad id explains why and posts nothing",
          settle(lambda: "no spaces" in txt("#ch-add-body")) and len(mine("/api/channels/people")) == before)
    pg.locator("#ch-add-ident").fill("999999999999999999")
    pg.locator("#ch-add-name").fill("Newcomer")
    check("P16: a valid id clears the warning", settle(lambda: pg.locator("#ch-add-ident").get_attribute("aria-invalid") == "false"))
    pg.locator("#ch-add-submit").click()
    check("P16: it posts exactly one add, id and name only", settle(lambda: len(mine("/api/channels/people")) == before + 1)
          and set(mine("/api/channels/people")[-1]) == {"platform", "ident", "name"})

    check("no script errors the whole way", not errs)
    pg.set_viewport_size({"width": 390, "height": 800})
    check("phone width: nothing scrolls sideways", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
    b.close()

ok = all(res)
print(f"\n{sum(res)} passed, {len(res) - sum(res)} failed")
sys.exit(0 if ok else 1)
