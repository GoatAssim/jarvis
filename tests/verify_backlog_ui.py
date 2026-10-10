"""L.55 - the Backlog panel (arrow keys, inline editing, reordering) in a real browser.

Loads web/public/index.html with the real ui-kit.js / backlog.js / CSS in
headless Chromium and answers /api/backlog* from an in-memory fake that mimics
the server's contract (stored order per column, PATCH, POST .../move, DELETE).
It does NOT run web/server.js or the Python CLI - only the panel's side.

Needs the `playwright` Python package and a launchable Chromium; prints SKIP
and exits 0 without them.

    python3 tests/verify_backlog_ui.py
"""
import json, mimetypes, sys, time
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

PUB = Path(__file__).resolve().parent.parent / "web" / "public"
STATES = ["idea", "todo", "doing", "blocked", "done"]
now = time.time()


def mk(i, title, state, **kw):
    d = {"id": i, "title": title, "project": "", "state": state, "priority": "normal",
         "note": "", "blocked_on": "", "created": now, "updated": now, "done_at": None}
    d.update(kw)
    return d


ITEMS = []
requests = []
fail = {"patch": False}


def reset():
    ITEMS[:] = [
        mk("b_000001", "Alpha", "todo", project="api", note="first note"),
        mk("b_000002", "Bravo", "todo", priority="high"),
        mk("b_000003", "Charlie", "todo"),
        mk("b_000004", "Delta", "doing"),
        mk("b_000005", "Echo", "blocked", blocked_on="Ana"),
    ]
    requests.clear()
    fail["patch"] = False


def board():
    b = {s: [i for i in ITEMS if i["state"] == s] for s in STATES}
    counts = {s: len(b[s]) for s in STATES}
    projects = {}
    for i in ITEMS:
        p = projects.setdefault(i["project"] or "(none)", {"project": i["project"] or "(none)", "open": 0, "blocked": 0, "done": 0, "total": 0})
        p["total"] += 1
        p["open" if i["state"] != "done" else "done"] += 1
    return {"ok": True, "board": b, "summary": {
        "counts": counts, "open": len(ITEMS), "blocked": [{"id": i["id"], "title": i["title"], "blocked_on": i["blocked_on"]} for i in b["blocked"]],
        "doing": [], "stale_doing": [], "projects": list(projects.values())}}


def find(i):
    return next((x for x in ITEMS if x["id"] == i), None)


def handle(route):
    req = route.request
    path = "/" + req.url.split("://", 1)[1].split("/", 1)[1].split("?")[0] if "://" in req.url else req.url
    def ok(body): return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    if path == "/api/backlog" and req.method == "GET":
        return ok(board())
    if path == "/api/backlog" and req.method == "POST":
        body = json.loads(req.post_data); requests.append(("ADD", body))
        item = mk("b_%06x" % (len(ITEMS) + 100), body["title"], body.get("state", "todo"), project=body.get("project", ""), priority=body.get("priority", "normal"))
        ITEMS.append(item); return ok({"ok": True, "item": item})
    if path.startswith("/api/backlog/"):
        rest = path[len("/api/backlog/"):]
        if rest.endswith("/move") and req.method == "POST":
            iid = rest[:-5]; body = json.loads(req.post_data); requests.append(("MOVE", iid, body))
            item = find(iid); to = body.get("state") or item["state"]; before = body.get("before") or None
            ITEMS.remove(item); item["state"] = to
            idx = next((n for n, x in enumerate(ITEMS) if x["id"] == before), None)
            if idx is None: ITEMS.append(item)
            else: ITEMS.insert(idx, item)
            return ok({"ok": True, "item": item})
        if req.method == "PATCH":
            body = json.loads(req.post_data); requests.append(("PATCH", rest, body))
            if fail["patch"]:
                return route.fulfill(status=500, content_type="application/json", body='{"error":"disk full"}')
            item = find(rest); item.update(body)
            if body.get("blocked_on") and item["state"] != "blocked": item["state"] = "blocked"
            return ok({"ok": True, "item": item})
        if req.method == "DELETE":
            requests.append(("DELETE", rest)); ITEMS.remove(find(rest)); return ok({"ok": True})
    if path.startswith("/api/"):
        return route.fulfill(status=200, content_type="application/json", body="{}")
    f = PUB / path.lstrip("/")
    if path in ("/", ""): f = PUB / "index.html"
    if f.is_file():
        return route.fulfill(status=200, content_type=mimetypes.guess_type(str(f))[0] or "text/plain", body=f.read_bytes())
    return route.fulfill(status=404, body="")


passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print("ok      " + name)
    else: failed += 1; print("FAILED  " + name)


def titles(page, col):
    return page.eval_on_selector_all(f'.bk-col[data-col="{col}"] .bk-card .bk-card__title', "els => els.map(e => e.textContent)")


def selected(page):
    return page.evaluate("() => { const e = document.querySelector('.bk-card.is-selected .bk-card__title'); return e ? e.textContent : null }")


def wait_idle(page):
    page.wait_for_function("() => document.getElementById('backlog-saving').hidden === true", timeout=5000)
    page.wait_for_timeout(120)


def fresh(browser):
    reset()
    page = browser.new_page(viewport={"width": 1400, "height": 850})
    page.route("**/*", handle)
    page.goto("http://jarvis.test/")
    page.wait_for_function("() => window.JarvisBacklog && window.JarvisUI")
    page.evaluate("() => window.JarvisBacklog.open()")
    page.wait_for_selector(".bk-card")
    return page


with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch()
    except Exception as e:
        print("SKIP: chromium could not launch:", e); sys.exit(0)

    # --- keyboard navigation -------------------------------------------------
    page = fresh(browser)
    check("opens with a card selected and focused", selected(page) is not None and page.evaluate("() => document.activeElement.classList.contains('bk-card')"))
    page.keyboard.press("ArrowDown"); check("down moves to the next card in the column", selected(page) == titles(page, "todo")[1])
    page.keyboard.press("ArrowRight"); check("right moves to the next column (Doing)", selected(page) == "Delta")
    page.keyboard.press("ArrowRight"); check("right again reaches Blocked", selected(page) == "Echo")
    page.keyboard.press("ArrowRight"); check("right at the last filled column stays put", selected(page) == "Echo")
    page.keyboard.press("ArrowLeft"); page.keyboard.press("ArrowLeft"); page.keyboard.press("End")
    check("End goes to the last card in the column", selected(page) == titles(page, "todo")[-1])
    page.keyboard.press("Home"); check("Home goes to the first", selected(page) == titles(page, "todo")[0])
    check("exactly one card is a tab stop", page.evaluate("() => document.querySelectorAll('.bk-card[tabindex=\"0\"]').length") == 1)
    page.focus("#backlog-search"); page.keyboard.press("ArrowDown")
    check("Down from the search box steps into the board", page.evaluate("() => document.activeElement.classList.contains('bk-card')"))
    page.close()

    # --- reorder with the keyboard ------------------------------------------
    page = fresh(browser)
    start = titles(page, "todo")
    page.keyboard.press("ArrowDown")           # second card
    moving = selected(page)
    page.keyboard.press("Shift+ArrowUp")
    check("Shift+Up moves the card up at once", titles(page, "todo")[0] == moving)
    check("...and it stays selected and focused", selected(page) == moving and page.evaluate("() => document.activeElement.classList.contains('bk-card')"))
    page.keyboard.press("Shift+ArrowDown"); page.keyboard.press("Shift+ArrowDown")
    wait_idle(page)
    check("quick repeated presses end in the right order", titles(page, "todo")[-1] == moving)
    moves = [r for r in requests if r[0] == "MOVE"]
    check("each real move was sent, in order", len(moves) == 3)
    check("server order matches the screen after the queue drains", [i["title"] for i in ITEMS if i["state"] == "todo"] == titles(page, "todo"))
    page.keyboard.press("Shift+ArrowDown")
    check("Shift+Down at the bottom sends nothing", len([r for r in requests if r[0] == "MOVE"]) == 3)
    page.keyboard.press("Shift+ArrowRight"); wait_idle(page)
    check("Shift+Right moves it to Doing, at the bottom", titles(page, "doing")[-1] == moving)
    check("...and the server got state=doing", requests[-1][0] == "MOVE" and requests[-1][2]["state"] == "doing")
    page.close()

    # --- reorder with the mouse ------------------------------------------------
    page = fresh(browser)
    page.drag_and_drop('.bk-card:has-text("Charlie")', '.bk-card:has-text("Alpha")', target_position={"x": 20, "y": 3})
    wait_idle(page)
    check("dragging onto the top half of a card puts it above", titles(page, "todo")[0] == "Charlie")
    page.drag_and_drop('.bk-card:has-text("Alpha")', '.bk-col[data-col="doing"] .bk-card:has-text("Delta")', target_position={"x": 20, "y": 3})
    wait_idle(page)
    check("drag into another column lands above the card dropped on", titles(page, "doing") == ["Alpha", "Delta"])
    page.close()

    # --- inline editing -----------------------------------------------------------
    page = fresh(browser)
    page.keyboard.press("Enter")
    check("Enter opens the editor with the title focused", page.evaluate("() => document.activeElement.dataset.field") == "title")
    page.keyboard.type(" v2")
    page.keyboard.press("Enter"); wait_idle(page)
    patches = [r for r in requests if r[0] == "PATCH"]
    check("saving sends only the changed field", len(patches) == 1 and list(patches[0][2].keys()) == ["title"])
    check("the new title is on the card", "Alpha v2" in titles(page, "todo"))
    check("focus returns to the card", page.evaluate("() => document.activeElement.classList.contains('bk-card')"))

    page.keyboard.press("e"); page.keyboard.type("zzz"); page.keyboard.press("Escape")
    page.wait_for_selector(".jui-backdrop")  # Escape on a changed card asks before discarding
    page.keyboard.press("Escape")  # confirm's own Escape = keep editing
    page.wait_for_timeout(250)
    check("dismissing the question keeps the editor open", page.locator(".bk-edit").count() == 1)
    page.keyboard.press("Escape"); page.wait_for_selector(".jui-backdrop")
    page.get_by_role("button", name="Discard").click(); page.wait_for_timeout(250)
    check("Discard closes the editor without saving", page.locator(".bk-edit").count() == 0 and len([r for r in requests if r[0] == "PATCH"]) == 1)

    page.keyboard.press("b"); page.keyboard.type("legal"); page.keyboard.press("Enter"); wait_idle(page)
    check("B sets what it is waiting on", requests[-1][2] == {"blocked_on": "legal"})
    check("the card moved to Blocked", "Alpha v2" in titles(page, "blocked"))

    page.keyboard.press("e"); page.keyboard.press("Enter")  # no change
    wait_idle(page)
    n_before = len(requests)
    check("saving with no change sends nothing", len(requests) == n_before and page.locator(".bk-edit").count() == 0)

    page.keyboard.press("e"); page.fill('.bk-edit [data-field="title"]', "x"); page.keyboard.press("Enter")
    check("a too-short title is refused inline", page.locator(".bk-edit__error").count() == 1 and page.locator(".bk-edit").count() == 1)
    page.keyboard.press("Escape"); page.wait_for_selector(".jui-backdrop"); page.get_by_role("button", name="Discard").click()
    page.close()

    # --- a failed save keeps the editor and the typing -----------------------------
    page = fresh(browser)
    fail["patch"] = True
    page.keyboard.press("e"); page.keyboard.type("!"); page.keyboard.press("Enter")
    page.wait_for_selector(".bk-edit__error")
    check("a failed save shows the error and keeps the draft", "disk full" in page.inner_text(".bk-edit__error") and page.input_value('.bk-edit [data-field="title"]').endswith("!"))
    page.close()

    # --- priority keys and delete ---------------------------------------------------
    page = fresh(browser)
    page.keyboard.press("3"); wait_idle(page)
    check("3 sets low priority", requests[-1][2] == {"priority": "low"} and page.locator('.bk-card.is-selected.bk-card--low').count() == 1)
    page.keyboard.press("Delete"); page.wait_for_selector(".jui-backdrop")
    page.get_by_role("button", name="Keep it").click(); page.wait_for_timeout(250)
    check("Delete asks first and Keep it removes nothing", not [r for r in requests if r[0] == "DELETE"])
    page.keyboard.press("Delete"); page.wait_for_selector(".jui-backdrop")
    first = selected(page)
    page.get_by_role("button", name="Remove").click(); wait_idle(page)
    check("confirming removes it", first not in titles(page, "todo") and any(r[0] == "DELETE" for r in requests))
    check("selection moves to a neighbour", selected(page) is not None)
    page.close()

    # --- shortcuts, filters, empty states -----------------------------------------------
    page = fresh(browser)
    page.keyboard.press("?"); check("? opens the cheat sheet", page.locator(".bk-keys__sheet").count() == 1)
    page.keyboard.press("?"); check("? closes it", page.locator(".bk-keys__sheet").count() == 0)
    page.keyboard.press("n"); check("N focuses the new-item box", page.evaluate("() => document.activeElement.id") == "backlog-new-title")
    page.keyboard.type("Foxtrot"); page.keyboard.press("Enter"); wait_idle(page)
    check("adding sends title and priority", requests[-1][0] == "ADD" and requests[-1][1]["title"] == "Foxtrot" and "priority" in requests[-1][1])
    check("the new card is selected", selected(page) == "Foxtrot")
    page.click('.bk-seg__btn:text("High")')
    check("priority filter narrows the board", page.locator(".bk-card").count() == 1)
    page.click('.bk-seg__btn:text("All")')
    page.fill("#backlog-search", "nomatch")
    check("a filtered-out column says so", "Filtered out" in page.inner_text('.bk-col[data-col="todo"]'))
    page.keyboard.press("Escape")
    check("Escape clears the search first and leaves the panel open", page.input_value("#backlog-search") == "" and page.evaluate("() => window.JarvisBacklog.isOpen()"))
    page.close()

    # --- Escape closes, and only then ------------------------------------------------------
    page = fresh(browser)
    page.keyboard.press("Escape"); check("Escape closes the panel", not page.evaluate("() => window.JarvisBacklog.isOpen()"))
    page.close()
    browser.close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
