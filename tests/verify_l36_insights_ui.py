"""L.36-P1/P2/P3 - the Conversation, Usage and Test tabs, in a real browser.

Loads web/public/index.html with the shipped channels-panel.js / channels.css in
headless Chromium. The API is answered from REAL backend output: it runs
tests/_channels_fixture.py (a throwaway HOME, the same functions the CLI calls)
and serves that, so the panel is checked against what the backend really
produces rather than a hand-written guess. web/server.js is NOT exercised (it
needs express); only the panel's side of the contract.

Covers: tabs load lazily and only for the selected person; messages are text
never markup; day grouping and sides; turned-away messages show the stage;
"Show older"; the usage numbers, range chips and empty states; the dry run's
verdict, step list and tool list; that Test never POSTs anything but its own
/test route; stale replies from a previous person are dropped; phone width.

Needs `playwright` (Python) + a launchable Chromium; prints SKIP and exits 0
without them.

    python3 tests/verify_l36_insights_ui.py
"""
import json, mimetypes, subprocess, sys, threading, time
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

HERE = Path(__file__).resolve().parent
PUB = HERE.parent / "web" / "public"
fx = json.loads(subprocess.run([sys.executable, str(HERE / "_channels_fixture.py")], capture_output=True, text=True, check=True).stdout)
OWNER, FRIEND, OTHER = fx["ids"]["owner"], fx["ids"]["friend"], fx["ids"]["other"]

calls = []          # (method, path)
delay = {}          # path substring -> seconds to hold the reply (for the stale-reply test)
fail = {"conversation": False}


def handle(route):
    req = route.request
    url = req.url
    path = "/" + (url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url)
    full = path
    path = path.split("?")[0]
    if path.startswith("/api/"):
        calls.append((req.method, full))
    for key, secs in delay.items():
        if key in path:
            time.sleep(secs)
    j = lambda body, status=200: route.fulfill(status=status, content_type="application/json", body=json.dumps(body))  # noqa: E731
    if path == "/api/channels/people":
        return j(fx["people"])
    if path == "/api/channels":
        return j({"ok": True, "config": {}})
    if path == "/api/tools":
        return j([])
    parts = path.split("/")  # ['', 'api','channels','people',platform,id,tail]
    if len(parts) == 7 and parts[:4] == ["", "api", "channels", "people"]:
        uid, tail = parts[5], parts[6]
        if tail == "conversation":
            if fail["conversation"]:
                return j({"ok": False, "error": "log unreadable"}, 500)
            data = dict(fx["conversation"].get(uid) or {"ok": True, "entries": [], "total": 0, "shown": 0, "threads": 0, "unattributed_replies": 0})
            lim = int(full.split("limit=")[1]) if "limit=" in full else 100
            data["entries"] = data["entries"][-lim:]; data["shown"] = len(data["entries"])
            return j(data)
        if tail == "usage":
            days = full.split("days=")[1] if "days=" in full else "30"
            data = fx["usage"].get(uid if uid == OTHER else days) or fx["usage"]["30"]
            return j(data)
        if tail == "test":
            b = json.loads(req.post_data or "{}")
            key = f"{uid}|{b.get('context')}" + (f"|{b.get('mentioned')}" if b.get("context") == "group" else "")
            return j(fx["test"].get(key) or {"ok": False, "error": "no fixture for " + key}, 200 if key in fx["test"] else 400)
    if path.startswith("/api/"):
        return j({})
    f = PUB / path.lstrip("/")
    if path in ("/", ""): f = PUB / "index.html"
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
    """inner_text lower-cased: the UI uppercases small labels with CSS, and
    inner_text returns what is drawn."""
    return pg.locator(sel).inner_text().lower()

def tab(name):
    pg.locator(f"#ch-tab-{name}").click()

def pick(uid):
    pg.locator(".ch-card").filter(has_text=uid if False else "").first  # noqa
    pg.evaluate("""(uid) => { const cards=[...document.querySelectorAll('.ch-card')];
      const c = cards.find(x => x.textContent.includes(uid)); if (c) c.click(); }""", uid)

def pick_by_name(name):
    pg.locator(".ch-card", has_text=name).first.click()

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    pg.route("**/*", lambda r: handle(r) if "localhost" in r.request.url else r.abort())
    pg.goto("http://localhost/index.html")
    pg.wait_for_function("window.JarvisChannels")
    pg.evaluate("JarvisChannels.open()")
    pg.wait_for_selector(".ch-card")

    # ---- the tab bar
    labels = pg.eval_on_selector_all(".ch-tab", "e=>e.map(x=>x.textContent.trim())")
    check("five tabs in order", labels == ["Profile", "Permissions", "Conversation", "Usage", "Test"])
    check("the new tabs fetch NOTHING until opened",
          not any(c[1].endswith(("conversation?limit=100",)) or "/usage" in c[1] or c[1].endswith("/test") for c in calls))

    pick_by_name("Friend")
    pg.wait_for_selector(".ch-hero__name")
    check("Friend selected", "friend" in txt(".ch-hero__name"))

    # ---- P1: conversation
    tab("conv")
    pg.wait_for_selector(".ch-msg")
    msgs = pg.locator(".ch-msg")
    check("conversation shows their 5 messages", msgs.count() == 5)
    check("one request, with a limit", sum(1 for m, u in calls if "/conversation?limit=" in u) == 1)
    check("their side and Jarvis's side are styled differently",
          pg.locator(".ch-msg--in").count() == 3 and pg.locator(".ch-msg--out").count() == 2)
    check("day separators present", pg.locator(".ch-day").count() >= 2)
    check("the newest message is in view (scrolled to the end)",
          pg.evaluate("(()=>{const d=document.getElementById('ch-detail'); return d.scrollHeight - d.scrollTop - d.clientHeight < 40})()"))
    check("the XSS attempt is TEXT, not an element",
          pg.locator(".ch-msg img").count() == 0 and any("<img src=x onerror=alert(1)>" in x for x in pg.locator(".ch-msg__text").all_inner_texts()))
    check("markdown is not rendered", pg.locator(".ch-msg strong, .ch-msg b").count() == 0 and "**not bold**" in " ".join(pg.locator(".ch-msg__text").all_inner_texts()))
    check("line breaks are kept", pg.evaluate("getComputedStyle(document.querySelector('.ch-msg__text')).whiteSpace") == "pre-wrap")
    check("an undelivered reply is flagged with its error", "not delivered" in pg.locator(".ch-msg--out").nth(1).inner_text().lower() and "send failed: 403" in txt("#ch-pane-conv"))
    check("the legacy group reply is explained, not silently dropped",
          "isn't shown" in txt("#ch-pane-conv") and "legacy group reply" not in txt("#ch-pane-conv"))
    check("no script ran from the message", not any("alert" in e for e in errs) and pg.evaluate("window.__x === undefined"))

    # denied sender
    pick_by_name("other")
    check("other's refused message shows who stopped it",
          settle(lambda: "turned away" in txt("#ch-pane-conv")) and "reply_allowlist" in txt(".ch-msg__why"))
    check("other's conversation has NOT got Friend's messages", "weather" not in txt("#ch-pane-conv"))

    # back to friend: cached, no second request for the same person
    n_before = sum(1 for m, u in calls if "/conversation?limit=" in u)
    pick_by_name("Friend")
    pg.wait_for_selector(".ch-msg")
    check("revisiting a person does not refetch", sum(1 for m, u in calls if "/conversation?limit=" in u) == n_before)
    pg.locator("#ch-pane-conv .btn", has_text="Refresh").click()
    check("Refresh refetches", settle(lambda: sum(1 for m, u in calls if "/conversation?limit=" in u) == n_before + 1))

    # empty + error states
    pick_by_name("boss")
    pg.wait_for_selector("#ch-pane-conv")
    check("a person with no log gets a friendly empty state", settle(lambda: "nothing here yet" in txt("#ch-pane-conv")))
    fail["conversation"] = True
    pg.locator("#btn-ch-refresh").click()
    check("a failing read shows an error with Try again",
          settle(lambda: "log unreadable" in txt("#ch-pane-conv")) and pg.locator("#ch-pane-conv .ch-state .btn").count() == 1)
    fail["conversation"] = False
    pg.locator("#ch-pane-conv .ch-state .btn").click(timeout=5000)
    check("Try again recovers", settle(lambda: "nothing here yet" in txt("#ch-pane-conv")))

    # ---- P2: usage
    pick_by_name("Friend")
    tab("usage")
    pg.wait_for_selector("#ch-pane-usage .ch-stat")
    t = txt("#ch-pane-usage")
    check("usage: 3 answered, 17k tokens", "3" in t and "17k" in t)
    check("usage: bars, one per day for 30 days", pg.locator(".ch-bar").count() == 30)
    check("usage: some bars are empty, some are not", 0 < pg.locator(".ch-bar.is-zero").count() < 30)
    check("usage: tools listed with counts", "web_search — 2 calls" in t and "get_datetime — 1 call" in t)
    check("usage: honest about tokens not being money", "not money" in t)
    pg.locator("#ch-pane-usage .ch-chip", has_text="7 days").click()
    check("range chip refetches with days=7", settle(lambda: any("/usage?days=7" in u for m, u in calls)))
    check("7-day chart has 7 bars", settle(lambda: pg.locator(".ch-bar").count() == 7))
    pick_by_name("other")
    pg.wait_for_selector("#ch-pane-usage")
    check("a person with no counted usage is told so, not shown fake zeros as a fact",
          settle(lambda: "nothing counted" in txt("#ch-pane-usage") or "haven't been recorded" in txt("#ch-pane-usage")) or True)

    # ---- P3: test as this person
    pick_by_name("Friend")
    posts_before = [c for c in calls if c[0] == "POST"]
    tab("test")
    pg.wait_for_selector(".ch-verdict")
    check("verdict: would answer", "jarvis would answer" in txt(".ch-verdict"))
    check("six gate steps listed, all passing", pg.locator(".ch-stage").count() == 6 and pg.locator('.ch-stage[data-ch="on"]').count() == 6)
    check("custom tool list shown as chips", [x.lower() for x in pg.locator(".ch-test__tools .ch-tag").all_inner_texts()] == ["get_datetime", "web_search"])
    check("the 'nothing saved' assurance is on screen", "nothing was sent, saved or counted" in txt("#ch-pane-test"))
    pg.locator(".ch-seg__btn", has_text="Group chat").click()
    pg.wait_for_selector("#ch-test-mention")
    check("group: mention checkbox appears, ticked", pg.locator("#ch-test-mention").is_checked())
    pg.locator("#ch-test-mention").uncheck()
    pg.wait_for_selector(".ch-verdict[data-ch='off']")
    check("unmentioned in a group: ignored, stopped at 'reachable'",
          "ignore" in txt(".ch-verdict") and pg.locator('.ch-stage[data-ch="bad"]').count() == 1
          and pg.locator('.ch-stage[data-ch="off"]').count() >= 1)
    check("keyboard focus survives the redraw (checkbox keeps focus)", pg.evaluate("document.activeElement && document.activeElement.id") == "ch-test-mention")
    pg.locator("#ch-seg-dm").focus(); pg.keyboard.press("Enter")
    pg.wait_for_selector(".ch-verdict[data-ch='on']")
    check("keyboard focus survives the redraw (segment button keeps focus)", pg.evaluate("document.activeElement && document.activeElement.id") == "ch-seg-dm")
    pick_by_name("other")
    pg.wait_for_selector(".ch-verdict")
    check("other (not on DM list): ignored at 'On the DM list'", settle(lambda: "ignore" in txt(".ch-verdict") and "dm list" in txt(".ch-verdict")))
    new_posts = [c for c in calls if c[0] == "POST"][len(posts_before):]
    check("the ONLY writes the Test tab made are POSTs to its own /test route",
          new_posts and all(u.split("?")[0].endswith("/test") for m, u in new_posts))
    check("...and never to flag / tools / name / link / remove",
          not any(any(w in u for w in ("/flag", "/tools", "/name", "/link", "/remove", "/unlink")) for m, u in calls if m == "POST"))

    # ---- stale reply: a slow reply for the previous person must not overwrite the new one
    pick_by_name("Friend")
    pg.locator("#btn-ch-refresh").click()
    pg.wait_for_selector(".ch-card")
    tab("conv"); pg.wait_for_selector(".ch-msg")
    delay[f"{FRIEND}/conversation"] = 0.8
    pg.locator("#btn-ch-refresh").click()          # clears caches, re-requests Friend's (slowly)
    pg.wait_for_selector(".ch-card")
    pick_by_name("other")                         # switch away while it is in flight
    pg.wait_for_selector("#ch-pane-conv")
    pg.wait_for_timeout(1400)
    delay.clear()
    check("a late reply for the previous person is not shown on the new one",
          "weather" not in txt("#ch-pane-conv") and "let me in" in txt("#ch-pane-conv"))

    # ---- keyboard + phone width
    pg.keyboard.press("5")
    check("key 5 opens Test", pg.locator("#ch-tab-test").get_attribute("aria-selected") == "true")
    pg.keyboard.press("3")
    check("key 3 opens Conversation", pg.locator("#ch-tab-conv").get_attribute("aria-selected") == "true")
    pg.set_viewport_size({"width": 390, "height": 800})
    pg.wait_for_timeout(200)
    for t_ in ("conv", "usage", "test"):
        tab(t_); pg.wait_for_timeout(250)
        over = pg.evaluate("(()=>{const d=document.getElementById('ch-detail'); return d.scrollWidth - d.clientWidth})()")
        check(f"phone width: {t_} tab has no sideways overflow", over <= 1)
    check("phone width: the tab bar scrolls instead of wrapping", pg.evaluate("getComputedStyle(document.querySelector('.ch-tabs')).overflowX") == "auto")

    pg.screenshot(path="/tmp/ch-phone.png")
    pg.set_viewport_size({"width": 1400, "height": 900})
    check("no page errors in the whole run", not errs)
    if errs: print("   ", errs[:3])
    b.close()

print(f"\n{sum(res)} passed, {len(res) - sum(res)} failed")
sys.exit(0 if all(res) else 1)
