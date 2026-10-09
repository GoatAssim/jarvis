"""L.8 - flipping Enabled or Autostart must not move anything in the Daemons panel.

Owner report (confirmed three times): disabling a service pops a notification
and the Daemons page shifts. Reproduced in headless Chromium: the "Disabled"
alert grew the summary by ~98px (tabs, the Quick-settings row the click had just
landed on and the pane below all moved) and the card's meta row appeared, pushing
every card below it down ~22px. The toast itself (fixed-position) was never the
cause.

This loads the shipped web/public/index.html with the real ui-kit.js /
daemons.js / CSS, answers /api/daemons and PATCH /api/daemons/:id from a small
stateful fake, flips each switch both ways and compares the bounding box of
every panel element before and after. Only the toast is allowed to appear.
Run at four window sizes, on both tabs, and with a service that has every other
badge so the badge row is as full as it gets. Also checks that the Enable button
that replaced the alert works, that hidden slots are inert, and that keyboard
focus stays on the switch that was used.

    python3 tests/verify_l8_toggle_layout.py
    JARVIS_PUB=/path/to/older/web/public python3 tests/verify_l8_toggle_layout.py   # prove it fails there

Needs the `playwright` Python package and a launchable Chromium; prints SKIP and
exits 0 without them. Does not exercise web/server.js.
"""
import json, mimetypes, os, sys
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

PUB = Path(os.environ.get("JARVIS_PUB") or Path(__file__).resolve().parent.parent / "web" / "public")
BASE = [
    {"id": "worker", "name": "worker", "builtin": False, "status": "running", "running": True, "pid": 11, "started_at": 1,
     "command": "python w.py", "description": "A background worker.", "enabled": True, "autostart": False,
     "restart": "on-failure", "shell": True, "supports_stdin": True, "adopted": True, "categories": ["media", "net"]},
    {"id": "other", "name": "other", "builtin": False, "status": "stopped", "command": "python o.py", "enabled": True},
    {"id": "third", "name": "third", "builtin": False, "status": "stopped", "command": "python t.py", "enabled": True},
]
data = []
patches = []

def reset():
    global data, patches
    data = [dict(d) for d in BASE]; patches = []

def handle(route):
    req = route.request; url = req.url
    path = "/" + (url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url).split("?")[0]
    if path == "/api/daemons" and req.method == "GET":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"daemons": data}))
    if path.startswith("/api/daemons/") and req.method == "PATCH":
        body = json.loads(req.post_data); patches.append(body)
        for d in data:
            if d["id"] == path.rsplit("/", 1)[1]: d.update(body)
        return route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
    if path.startswith("/api/"):
        return route.fulfill(status=200, content_type="application/json", body="{}")
    f = PUB / path.lstrip("/")
    if path in ("/", ""): f = PUB / "index.html"
    if f.is_file():
        return route.fulfill(status=200, content_type=mimetypes.guess_type(str(f))[0] or "text/plain", body=f.read_bytes())
    return route.fulfill(status=404, body="")

# Every panel element a user would see move, keyed so before/after line up.
SNAP = """() => {
  const out = {};
  const put = (k, e) => { if (!e) return; const b = e.getBoundingClientRect();
    out[k] = [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
  const one = (k, s) => put(k, document.querySelector(s));
  document.querySelectorAll('.dmn-card').forEach(c => { put('card:' + c.dataset.id, c); put('cardrow:' + c.dataset.id, c.querySelector('.dmn-card__row')); });
  ['.dmn-summary', '.dmn-hero', '.dmn-title-row', '.dmn-badges', '.dmn-alerts', '.dmn-tabs', '.dmn-detail-scroll', '#dmn-main', '.dmn-body',
   '.dmn-detail', '.dmn-section-title'].forEach(s => one(s, s));
  document.querySelectorAll('.dmn-act').forEach(e => put('act:' + e.dataset.act, e));
  document.querySelectorAll('.dmn-tab').forEach(e => put('tab:' + e.dataset.tab, e));
  document.querySelectorAll('.dmn-detail .dmn-switch').forEach((e, i) => put('switch' + i, e));
  document.querySelectorAll('.dmn-body > *').forEach((e, i) => put('pane' + i, e));
  const sc = {};
  ['.dmn-detail-scroll', '.dmn-body', '#dmn-main'].forEach(s => { const e = document.querySelector(s); if (e) sc[s] = e.scrollTop; });
  sc.doc = document.scrollingElement.scrollTop;
  return { boxes: out, scroll: sc };
}"""

res = []
def check(name, cond, extra=""):
    res.append(bool(cond)); print(("ok      " if cond else "FAILED  ") + name + ("" if cond else ("   " + extra if extra else "")))

def moved(a, b):
    out = []
    for k in sorted(set(a["boxes"]) | set(b["boxes"])):
        if a["boxes"].get(k) != b["boxes"].get(k):
            out.append(f"{k}: {a['boxes'].get(k)} -> {b['boxes'].get(k)}")
    for k in a["scroll"]:
        if a["scroll"][k] != b["scroll"].get(k): out.append(f"scrollTop {k}: {a['scroll'][k]} -> {b['scroll'].get(k)}")
    return out

def open_panel(pg, tab):
    pg.goto("http://localhost/index.html"); pg.wait_for_function("window.JarvisDaemons")
    pg.evaluate("JarvisDaemons.open()"); pg.wait_for_selector(".dmn-summary .dmn-title")
    pg.locator('.dmn-card[data-id="worker"]').click(); pg.wait_for_timeout(300)
    pg.click(f'.dmn-tab[data-tab="{tab}"]')
    # park the pointer over nothing: a hovered card slides 2px (a transition,
    # not layout) and would show up as a "move" if a snapshot caught it mid-way
    pg.mouse.move(700, 5); pg.wait_for_timeout(400)

def switch(pg, label):
    return pg.locator(".dmn-detail .dmn-switch").filter(has_text=label).locator("input")

with sync_playwright() as p:
    b = p.chromium.launch()
    errs = []
    for (w, h) in [(1400, 900), (1100, 700), (800, 700), (500, 800)]:
        for tab in ("details", "console"):
            reset()
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.route("**/*", lambda r: handle(r) if "localhost" in r.request.url else r.abort())
            open_panel(pg, tab)
            for label, key in (("Enabled", "enabled"), ("Autostart", "autostart")):
                for _ in range(2):  # off then back on (or on then off)
                    before = pg.evaluate(SNAP)
                    n = len(patches)
                    switch(pg, label).evaluate("e => e.click()")
                    pg.wait_for_selector(".jui-toast", timeout=3000)
                    pg.wait_for_timeout(450)  # past the PATCH, the reload and the re-render
                    after = pg.evaluate(SNAP)
                    diff = moved(before, after)
                    check(f"{w}x{h} {tab}: flipping {label} moves nothing ({len(patches)-n} PATCH)", not diff and len(patches) == n + 1, "; ".join(diff[:6]))
            pg.close()

    # behaviour around the change
    reset()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: handle(r) if "localhost" in r.request.url else r.abort())
    open_panel(pg, "details")
    slot = pg.locator('.dmn-summary [data-slot="disabled"]')
    check("enabled service: Disabled slot exists but is hidden and inert",
          slot.count() == 1 and not slot.is_visible() and slot.is_disabled() and slot.get_attribute("aria-hidden") == "true")
    check("enabled service: no Disabled alert", pg.locator(".dmn-alert").filter(has_text="Disabled").count() == 0)
    check("enabled service: no Disabled mark on the card", pg.locator('.dmn-card[data-id="worker"] .dmn-mark.is-on').count() == 0)

    switch(pg, "Enabled").evaluate("e => e.click()"); pg.wait_for_timeout(500)
    check("disabling sends enabled:false", patches[-1] == {"enabled": False})
    check("disabled service: the Disabled slot shows and is the Enable button", slot.is_visible() and slot.is_enabled() and "enable" in slot.inner_text().lower())
    check("disabled service: still no in-flow Disabled alert", pg.locator(".dmn-alert").filter(has_text="Disabled").count() == 0)
    check("disabled service: card shows the Disabled mark", pg.locator('.dmn-card[data-id="worker"] .dmn-mark--off.is-on').count() == 1)
    check("Stop/Start toggle still offers Stop for the running disabled daemon", pg.locator('.dmn-act[data-act="stop"]').count() == 1)

    before = pg.evaluate(SNAP)
    slot.click(); pg.wait_for_timeout(500)
    after = pg.evaluate(SNAP)
    check("Enable slot sends enabled:true", patches[-1] == {"enabled": True})
    check("enabling through the slot moves nothing either", not moved(before, after), "; ".join(moved(before, after)[:6]))
    check("after enabling, the slot is hidden again", not slot.is_visible())

    # keyboard focus stays on the switch that was used
    pg.locator(".dmn-detail .dmn-switch").filter(has_text="Autostart").locator("input").focus()
    pg.keyboard.press("Space"); pg.wait_for_timeout(500)
    focused = pg.evaluate("(document.activeElement.closest('label')||{}).innerText || ''")
    check("focus stays on the Autostart switch after it re-renders", "Autostart" in focused, focused[:40])
    check("Autostart mark shows on the card and the badge in the summary", pg.locator('.dmn-card[data-id="worker"] .dmn-mark--auto.is-on').count() == 1
          and pg.locator('.dmn-summary [data-slot="autostart"]').is_visible())
    check("no page errors", not errs, "; ".join(errs[:3]))
    b.close()

print(f"\n{sum(res)}/{len(res)} checks passed")
sys.exit(0 if all(res) else 1)
