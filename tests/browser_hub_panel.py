#!/usr/bin/env python3
"""Run the REAL Downloads panel in a REAL browser engine against the REAL hub back end (brief 24).

What it starts:
  * the real web/server.js (needs `npm install` done once in web/: express and ws),
  * which runs the real `python -m jarvis hub-admin ...` (jarvis/hub_admin.py, the real store, the real
    `reorder`, the live stream from `watch_events`),
  * a temporary HOME, so nothing of yours is read or written,
  * headless Chromium through Playwright, opening the real index.html and Menu > Downloads.

What it checks (N15, N16, N17):
  * three queued rows show the grip and both arrows once the live stream is up, and STAY that way;
  * each arrow and a drag (synthetic drag events) changes the order on screen within a second, the request is
    accepted, the stored priorities follow, and the order survives a reload;
  * a row that cannot be reordered says why in its tooltip;
  * the toggle and Cancel measure the same height, on one line, on every state that shows both;
  * the Sources tab lists every engine and every provider with its state.

If anything it needs is missing (Playwright, a browser engine, node, web/node_modules) it PRINTS a line saying
so and exits 0.  A skip is never silent, and it is not a pass: nothing was seen in a browser.

    python tests/browser_hub_panel.py            # run it
    python tests/browser_hub_panel.py --keep     # leave the temp HOME and print its path
"""

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "jarvis-cli"
WEB = ROOT / "web"
NAME = "browser_hub_panel"
KEEP = "--keep" in sys.argv


def skip(why):
    print("SKIPPED %s: %s. Nothing was seen in a browser; this is not a pass." % (NAME, why))
    sys.exit(0)


try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # noqa: BLE001
    skip("Playwright for Python is not installed (%s)" % exc.__class__.__name__)
if not shutil.which("node"):
    skip("node is not on PATH")
if not (WEB / "node_modules" / "express").is_dir():
    skip("web/node_modules is missing (run `npm install` in web/ once)")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name + ((" -- " + str(detail)) if detail != "" else ""))


# --------------------------------------------------------------------------- the temporary world

HOME = Path(tempfile.mkdtemp(prefix="jarvis-browser-"))
os.environ["HOME"] = str(HOME)
os.environ["USERPROFILE"] = str(HOME)
sys.path.insert(0, str(PKG))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()
BASE = "http://127.0.0.1:%d" % PORT
server = None


def kill_pid(pid):
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


def kill_daemons():
    """The web server's startup tick autostarts the hub daemon.  It must not run here: it would start the
    queued rows before the page could show them waiting.  Stop whatever the temporary HOME started."""
    for status in (HOME / ".jarvis" / "daemons").glob("*/status.json"):
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        kill_pid(data.get("child_pid"))
        kill_pid(data.get("supervisor_pid"))


def fetch(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=(json.dumps(body).encode() if body is not None else None))
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def start_server():
    global server
    env = dict(os.environ)
    env.update({"PORT": str(PORT), "JARVIS_BRIDGE": "0", "PYTHONPATH": str(PKG) + os.pathsep + env.get("PYTHONPATH", ""),
                "PYTHONIOENCODING": "utf-8"})
    server = subprocess.Popen(["node", "server.js"], cwd=str(WEB), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 60
    while time.time() < deadline:
        if server.poll() is not None:
            skip("web/server.js exited at once (is `npm install` done in web/?)")
        try:
            fetch("/api/hub/list")
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    skip("web/server.js did not answer within 60 s")


def seed():
    """Three waiting rows (queued) in a known order, and one row in each of the other states the buttons depend on.
    States are set straight in the temporary database: nothing is running, so nothing can move them."""
    from jarvis.hub.store import Store

    st = Store()
    ids = {}
    for key, title in (("a", "Alpha waiting"), ("b", "Bravo waiting"), ("c", "Charlie waiting")):
        ids[key] = st.add_item(engine="http", source_url="http://example.invalid/%s.bin" % key, title=title)[0]["id"]
        time.sleep(0.05)
    ids["paused"] = st.add_item(engine="http", source_url="http://example.invalid/p.bin", title="Paused one", state="paused")[0]["id"]
    ids["sched"] = st.add_item(engine="http", source_url="http://example.invalid/s.bin", title="Scheduled one", state="paused",
                               options={"start_at": time.time() + 3600})[0]["id"]
    for key, title in (("run", "Running one"), ("stall", "Stalled one"), ("fail", "Failed one")):
        ids[key] = st.add_item(engine="http", source_url="http://example.invalid/%s.bin" % key, title=title)[0]["id"]
    st.register_provider("steam", label="Steam", mode="poll", kind="game", icon="S", capabilities=[], interval_s=60, source_file="hub.steam")
    far = time.time() + 3600
    con = sqlite3.connect(str(st.path))
    con.execute("UPDATE items SET next_attempt_at=? WHERE id=?", (far, ids["sched"]))
    con.execute("UPDATE items SET state='downloading', percent=40, bytes_done=400, bytes_total=1000, speed_bps=100, started_at=? WHERE id=?",
                (time.time(), ids["run"]))
    con.execute("UPDATE items SET state='stalled', percent=10, next_attempt_at=? WHERE id=?", (far, ids["stall"]))
    con.execute("UPDATE items SET state='failed', error='could not resolve example.invalid', finished_at=? WHERE id=?", (time.time(), ids["fail"]))
    con.commit()
    con.close()
    return ids


# --------------------------------------------------------------------------- driving the page

HUB_ROOT = """window.__hubRoot = () => { for (const h of document.querySelectorAll('*')) { if (h.shadowRoot && h.shadowRoot.querySelector('#liveTag')) return h.shadowRoot; } return document; };"""


def open_panel(page):
    page.add_init_script(HUB_ROOT)          # the panel lives in a shadow root; page scripts find it through this
    page.goto(BASE + "/")
    try:                                                   # the first-run Setup overlay, when it shows (it checks first)
        page.wait_for_selector("#btn-setup-skip", state="visible", timeout=10000)
        page.click("#btn-setup-skip")
        page.wait_for_selector("#setup-overlay", state="hidden", timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    page.click("#btn-panel-menu")
    page.get_by_text("Everything downloading, queued, failed or finished").click()
    page.wait_for_selector(".row[data-id]", timeout=20000)
    page.wait_for_selector("#liveTag:has-text('Live')", timeout=20000)
    page.wait_for_timeout(1500)       # past the first poll: the old bug was a slow poll landing AFTER the stream


def titles(page, only_queued=True):
    return page.evaluate("""(onlyQueued) => Array.from(__hubRoot().querySelectorAll('.row[data-id]'))
        .filter((r) => !onlyQueued || r.classList.contains('state-queued'))
        .map((r) => r.querySelector('.row__title').textContent)""", only_queued)


def wait_titles(page, want, ms=700):
    deadline = time.time() + ms / 1000.0
    got = None
    while time.time() < deadline:
        got = titles(page)
        if got == want:
            return True, got
        page.wait_for_timeout(50)
    return got == want, got


def stored_order(ids):
    from jarvis.hub.store import Store

    rows = Store().list_items(order="queue", owner="hub", states_in=("queued",))
    return [r["title"] for r in rows]


def wait_stored(page, ids, want, ms=4000):
    """The screen follows a click at once; the hub's stored order catches up when its answer comes back."""
    deadline = time.time() + ms / 1000.0
    got = None
    while time.time() < deadline:
        got = stored_order(ids)
        if got == want:
            return True, got
        page.wait_for_timeout(100)
    return got == want, got


def row(page, title):
    return page.locator(".row[data-id]", has=page.locator(".row__title", has_text=title)).first


def synthetic_drag(page, source_title, target_title, after):
    page.evaluate("""([src, dst, after]) => {
        const find = (t) => Array.from(__hubRoot().querySelectorAll('.row[data-id]')).find((r) => r.querySelector('.row__title').textContent === t);
        const a = find(src), b = find(dst);
        const dt = new DataTransfer();
        const box = b.getBoundingClientRect();
        const y = after ? box.top + box.height * 0.8 : box.top + box.height * 0.2;
        const ev = (type, target) => target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt, clientX: box.left + 20, clientY: y }));
        ev('dragstart', a); ev('dragenter', b); ev('dragover', b); ev('drop', b); ev('dragend', a);
    }""", [source_title, target_title, after])


def section_reorder(page, ids):
    print("\n-- N15: the arrows and the drag --")
    want0 = ["Alpha waiting", "Bravo waiting", "Charlie waiting"]
    # the panel sorts waiting rows by priority, then oldest first
    got0 = titles(page)
    check("three waiting rows are on the page, oldest first", got0 == want0, got0)
    drag = page.evaluate("""() => Array.from(__hubRoot().querySelectorAll('.row.state-queued')).map((r) => ({
        draggable: r.getAttribute('draggable'), grip: !!r.querySelector('.row__grip'),
        arrows: Array.from(r.querySelectorAll('.rbtn')).map((b) => b.textContent).filter((t) => t === '\\u2191' || t === '\\u2193')}))""")
    check("each waiting row is draggable, has the grip and both arrows (this was missing: a slow poll overwrote the stream's rows)",
          all(d["draggable"] == "true" and d["grip"] and len(d["arrows"]) == 2 for d in drag), drag)
    first_up = row(page, "Alpha waiting").get_by_role("button", name="Move up in the queue", exact=False)
    last_down = row(page, "Charlie waiting").get_by_role("button", name="Move down in the queue", exact=False)
    check("the first row's up arrow and the last row's down arrow are off", first_up.is_disabled() and last_down.is_disabled())

    seen = []
    page.on("request", lambda r: seen.append(r) if r.url.endswith("/api/hub/reorder") else None)
    responses = []
    page.on("response", lambda r: responses.append((r.status, r.request.post_data)) if r.url.endswith("/api/hub/reorder") else None)

    # arrow down on the first row
    row(page, "Alpha waiting").get_by_role("button", name="Move down in the queue", exact=False).click()
    ok, got = wait_titles(page, ["Bravo waiting", "Alpha waiting", "Charlie waiting"])
    check("the down arrow moves the row down on screen within a second", ok, got)
    ok, got = wait_stored(page, ids, ["Bravo waiting", "Alpha waiting", "Charlie waiting"])
    check("... and the hub stored it: the queue the daemon would read is the same order", ok, got)
    for _ in range(40):
        if responses:
            break
        page.wait_for_timeout(100)
    check("... and the request was accepted (200) and carried the whole waiting queue in the new order",
          bool(responses) and responses[-1][0] == 200 and len(json.loads(responses[-1][1])["ids"]) == 3, responses)

    # arrow up on the last row
    row(page, "Charlie waiting").get_by_role("button", name="Move up in the queue", exact=False).click()
    ok, got = wait_titles(page, ["Bravo waiting", "Charlie waiting", "Alpha waiting"])
    check("the up arrow moves the row up on screen within a second", ok, got)
    ok, got = wait_stored(page, ids, ["Bravo waiting", "Charlie waiting", "Alpha waiting"])
    check("... and the hub stored it", ok, got)

    # a drag: the first row dropped below the last one
    synthetic_drag(page, "Bravo waiting", "Alpha waiting", True)
    ok, got = wait_titles(page, ["Charlie waiting", "Alpha waiting", "Bravo waiting"])
    check("dragging a card below another changes the order on screen within a second", ok, got)
    ok, got = wait_stored(page, ids, ["Charlie waiting", "Alpha waiting", "Bravo waiting"])
    check("... and the hub stored it", ok, got)
    # a drag the other way: the last row dropped above the first
    synthetic_drag(page, "Bravo waiting", "Charlie waiting", False)
    ok, got = wait_titles(page, ["Bravo waiting", "Charlie waiting", "Alpha waiting"])
    check("dragging a card above another works too", ok, got)
    ok, got = wait_stored(page, ids, ["Bravo waiting", "Charlie waiting", "Alpha waiting"])
    check("... and the hub stored it", ok, got)

    page.reload()
    open_panel(page)
    check("the order survives a reload", titles(page) == ["Bravo waiting", "Charlie waiting", "Alpha waiting"], titles(page))
    check("... and the rows are still draggable after the reload",
          page.evaluate("Array.from(__hubRoot().querySelectorAll('.row.state-queued')).every((r) => r.getAttribute('draggable') === 'true')"))

    print("\n-- N15: a row that can't be reordered says why --")
    for title, word in (("Running one", "started"), ("Paused one", "paused"), ("Stalled one", "started")):
        tip = row(page, title).get_attribute("title") or ""
        check("%s: the card's tooltip explains it (%r)" % (title, word), word in tip.lower() and row(page, title).get_attribute("draggable") == "false", tip)
    check("a finished row has no reorder tooltip (the idea doesn't apply)", (row(page, "Failed one").get_attribute("title") or "") == "")
    check("a waiting row has no such tooltip (it CAN be moved)", (row(page, "Alpha waiting").get_attribute("title") or "") == "")


def heights(page, title):
    return page.evaluate("""(title) => {
        const r = Array.from(__hubRoot().querySelectorAll('.row[data-id]')).find((x) => x.querySelector('.row__title').textContent === title);
        const box = (el) => { const b = el.getBoundingClientRect(); return { top: Math.round(b.top * 10) / 10, h: Math.round(b.height * 10) / 10, w: Math.round(b.width) }; };
        const t = r.querySelector('.tbtn');
        const cancel = Array.from(r.querySelectorAll('.rbtn')).find((b) => b.textContent === 'Cancel');
        const retry = Array.from(r.querySelectorAll('.rbtn')).find((b) => b.textContent === 'Retry');
        return { toggle: t && box(t), cancel: cancel && box(cancel), retry: retry && box(retry), card: Math.round(r.getBoundingClientRect().height),
                 toggleFirst: !!t && !!cancel && !!(t.compareDocumentPosition(cancel) & Node.DOCUMENT_POSITION_FOLLOWING) };
    }""", title)


def section_buttons(page):
    print("\n-- N16: the toggle and Cancel are the same height, on one row --")
    for title in ("Alpha waiting", "Running one", "Paused one", "Stalled one", "Scheduled one"):
        m = heights(page, title)
        if not (m["toggle"] and m["cancel"]):
            check("%s shows a toggle and Cancel" % title, False, m)
            continue
        check("%s: toggle %.1f px and Cancel %.1f px tall (same height)" % (title, m["toggle"]["h"], m["cancel"]["h"]), abs(m["toggle"]["h"] - m["cancel"]["h"]) < 0.6, m)
        check("%s: on the same line, toggle first" % title, abs(m["toggle"]["top"] - m["cancel"]["top"]) < 1.5 and m["toggleFirst"], m)
        if m["retry"]:
            check("%s: Retry shares the row and the height" % title, abs(m["retry"]["top"] - m["toggle"]["top"]) < 1.5 and abs(m["retry"]["h"] - m["toggle"]["h"]) < 0.6, m)
    stalled = heights(page, "Stalled one")
    check("the stalled row has toggle, Retry and Cancel together", bool(stalled["retry"] and stalled["toggle"] and stalled["cancel"]), stalled)
    failed = heights(page, "Failed one")
    check("a failed row (Retry only) still shows its toggle", bool(failed["toggle"]), failed)
    tall = max(heights(page, t)["card"] for t in ("Alpha waiting", "Running one", "Paused one", "Stalled one"))
    check("no card is taller than 130 px (the buttons did not make it grow); tallest %d px" % tall, tall <= 130, tall)

    # the detail pane
    row(page, "Stalled one").click()
    page.wait_for_selector("#detail:not([hidden]) .detail__actions--top", timeout=8000)
    d = page.evaluate("""() => {
        const box = (el) => { const b = el.getBoundingClientRect(); return { top: Math.round(b.top * 10) / 10, h: Math.round(b.height * 10) / 10 }; };
        const bar = __hubRoot().querySelector('#detail .detail__actions--top');
        const bs = Array.from(bar.children);
        const by = (t) => bs.find((b) => b.textContent.indexOf(t) > -1);
        return { toggle: by('Pause') && box(by('Pause')), retry: by('Retry') && box(by('Retry')), cancel: by('Cancel') && box(by('Cancel')), order: bs.map((b) => b.textContent.trim()) };
    }""")
    check("the detail pane: toggle, Retry and Cancel are the same height and on one line, toggle first",
          bool(d["toggle"] and d["retry"] and d["cancel"]) and abs(d["toggle"]["h"] - d["cancel"]["h"]) < 0.6 and abs(d["toggle"]["h"] - d["retry"]["h"]) < 0.6
          and abs(d["toggle"]["top"] - d["cancel"]["top"]) < 1.5 and "Pause" in d["order"][0], d)


def section_sources(page, ids):
    print("\n-- N17: the Sources tab --")
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method != "GET" and "/api/hub/sources" in r.url else None)
    # a qBittorrent file with a password in it: it must never reach the page
    secret = "hunter2-SECRET-pw"
    (HOME / ".jarvis").mkdir(parents=True, exist_ok=True)
    (HOME / ".jarvis" / "qbittorrent.json").write_text(json.dumps({"address": "http://127.0.0.1:1", "username": "admin", "password": secret}), encoding="utf-8")
    page.click("#tabSources")
    page.wait_for_selector(".src-engine", timeout=30000)
    page.wait_for_timeout(500)
    cards = page.evaluate("""() => Array.from(__hubRoot().querySelectorAll('.src-engine')).map((c) => ({
        id: c.getAttribute('data-id'), state: c.getAttribute('data-state'), word: c.querySelector('.src__state').textContent,
        text: c.innerText, configure: !!c.querySelector('.src-configure'), check: !!c.querySelector('.src-check')}))""")
    by = {c["id"]: c for c in cards}
    check("all four engines have a card: http, ytdlp, stremio, qbittorrent", {"http", "ytdlp", "stremio", "qbittorrent"} <= set(by), sorted(by))
    check("each card has the state word and Check now", all(c["word"] in ("Ready", "Needs setup", "Not running", "Off", "Broken") and c["check"] for c in cards), cards)
    check("HTTP is Ready (nothing in the way)", by["http"]["word"] == "Ready", by["http"])
    check("Stremio, with no server running here, says Not running and gives the sentence", by["stremio"]["word"] == "Not running" and "isn't running" in by["stremio"]["text"], by["stremio"]["text"])
    check("qBittorrent says what it needs and names its file, and has no Configure button", "qbittorrent.json" in by["qbittorrent"]["text"] and not by["qbittorrent"]["configure"], by["qbittorrent"]["text"])
    check("Stremio, HTTP and yt-dlp have a Configure button", all(by[k]["configure"] for k in ("stremio", "http", "ytdlp")))
    heads = page.evaluate("Array.from(__hubRoot().querySelectorAll('#srcList .src__title')).map((h) => h.textContent)")
    check("two sections, in order", heads == ["Download engines", "Programs that report their own downloads"], heads)
    play = page.evaluate("(() => { const c = __hubRoot().querySelector('.src-provider[data-id=playnite]'); return c ? c.innerText : ''; })()")
    check("Playnite has never reported, and its card says when it will appear", "first install Jarvis starts through Playnite" in play, play)
    steam = page.evaluate("(() => { const c = __hubRoot().querySelector('.src-provider[data-id=steam]'); return c ? { text: c.innerText, buttons: Array.from(c.querySelectorAll('button')).map((b) => b.textContent) } : null; })()")
    check("the second section keeps the provider controls: switch, Settings, facts", bool(steam) and "Turn off" in steam["buttons"] and any("Settings" in b for b in steam["buttons"]) and "Downloads:" in steam["text"], steam)
    page_text = page.evaluate("__hubRoot().querySelector('#sourcesPane').innerText")
    check("no password, address or home folder anywhere on the tab", secret not in page_text and "127.0.0.1" not in page_text and "http://" not in page_text and str(HOME) not in page_text)

    # Check now: reads again, changes nothing, and says it is working while it does
    before = sorted((r["title"], r["state"], r["priority"]) for r in __import__("jarvis.hub.store", fromlist=["Store"]).Store().list_items(limit=100, include_dismissed=True))
    page.click("#srcRefresh")
    saw = False
    for _ in range(20):
        txt = page.inner_text("#srcRefresh")
        if "Checking" in txt:
            saw = True
            break
        page.wait_for_timeout(50)
    page.wait_for_selector("#srcRefresh:has-text('Check now')", timeout=30000)
    after = sorted((r["title"], r["state"], r["priority"]) for r in __import__("jarvis.hub.store", fromlist=["Store"]).Store().list_items(limit=100, include_dismissed=True))
    check("Check now changes nothing: no write request, and every row is as it was", not posts and before == after, posts)

    # Configure lands on the block
    def land(engine, anchor):
        page.locator('#sourcesPane .src-engine[data-id=%s] .src-configure' % engine).click()
        page.wait_for_selector('#prefs-backdrop:not([hidden]) [data-settings-anchor="%s"]' % anchor, timeout=20000)
        page.wait_for_timeout(900)
        box = page.evaluate("""(a) => { const n = document.querySelector('[data-settings-anchor="' + a + '"]'); const r = n.getBoundingClientRect();
            const pane = n.closest('.prefs-pane') || n.parentElement; const pr = pane.getBoundingClientRect();
            return { top: r.top, bottom: r.bottom, vh: window.innerHeight, paneTop: pr.top, section: document.querySelector('#prefs-rail [aria-current=page]') && document.querySelector('#prefs-rail [aria-current=page]').dataset.section }; }""", anchor)
        return box
    box = land("stremio", "stremio")
    check("Stremio's Configure opens Settings > Downloads with the Stremio card in view", box["section"] == "downloads" and 0 <= box["top"] < box["vh"] and box["bottom"] > 0, box)
    page.click("#skin-close")
    page.wait_for_selector("#prefs-backdrop", state="hidden", timeout=10000)
    box = land("http", "http")
    opened = page.evaluate("document.querySelector('[data-settings-anchor=\"http\"]').open")
    check("HTTP's Configure opens Settings > Downloads at HTTP's own block, and unfolds it", box["section"] == "downloads" and 0 <= box["top"] < box["vh"] and opened is True, (box, opened))
    page.click("#skin-close")
    page.wait_for_selector("#prefs-backdrop", state="hidden", timeout=10000)
    box = land("ytdlp", "yt-dlp")
    check("yt-dlp's Configure lands on its block too", box["section"] == "downloads" and 0 <= box["top"] < box["vh"], box)
    page.click("#skin-close")
    page.wait_for_selector("#prefs-backdrop", state="hidden", timeout=10000)




def section_settings(page):
    print("\n-- N20: Settings > Downloads, rebuilt --")
    from jarvis.hub.store import Store

    page.click("#settingsBtn")
    page.wait_for_selector("#prefs-backdrop:not([hidden]) .dls .dls-rail__item", timeout=30000)
    page.wait_for_timeout(400)
    rail = page.evaluate("Array.from(document.querySelectorAll('.dls-rail__item')).map((b) => b.firstChild.textContent)")
    check("the page has its own rail with the seven sections", rail[:7] == ["Files", "Speed and schedule", "Running at once", "Sources", "Notifications", "When a download finishes", "Safety"], rail)
    blocks = page.evaluate("Array.from(document.querySelectorAll('details.dls-source')).map((d) => [d.getAttribute('data-settings-anchor'), d.open])")
    check("each engine has a collapsed block under Sources", [b[0] for b in blocks][:4] == ["http", "qbittorrent", "stremio", "yt-dlp"] and not any(b[1] for b in blocks), blocks)
    page.click("details[data-settings-anchor=http] > summary")
    check("a block unfolds on click and shows its settings", page.evaluate("document.querySelector('details[data-settings-anchor=http]').open") is True
          and page.locator("details[data-settings-anchor=http] .prefs-row").count() >= 3)
    page.click("details[data-settings-anchor=http] > summary")
    # search
    total = page.locator(".dls .prefs-row:visible").count()
    page.fill(".dls-search", "speed")
    page.wait_for_timeout(200)
    shown = page.evaluate("Array.from(document.querySelectorAll('.dls .prefs-row, .dls .prefs-group-table')).filter((r) => !r.hidden && r.offsetParent !== null).map((r) => r.querySelector('.prefs-row__label').textContent)")
    check("typing 'speed' leaves only the speed settings on screen", 0 < len(shown) < 5 and any("Speed limit" in x for x in shown), shown)
    page.fill(".dls-search", "zzzz nothing")
    page.wait_for_timeout(150)
    check("a search nothing matches says so", page.locator(".dls-none:visible").count() == 1)
    page.fill(".dls-search", "")
    page.wait_for_timeout(150)
    check("clearing the search brings every row back", page.locator(".dls .prefs-row:visible").count() >= total - 1, (total, page.locator(".dls .prefs-row:visible").count()))
    # change a setting, see it differ, reset it
    before = Store().get_settings()["concurrency"]["total"]
    box = page.locator('.dls .prefs-row:has-text("Downloads at once (all)") input[type=number]')
    box.fill("7")
    box.press("Tab")
    page.wait_for_selector('.dls .prefs-row:has-text("Downloads at once (all)") .prefs-row__status.is-ok', timeout=20000)
    check("changing a row saves it through the hub", Store().get_settings()["concurrency"]["total"] == 7, Store().get_settings()["concurrency"])
    default_text = page.evaluate("document.evaluate(\"//div[contains(@class,'prefs-row')][.//div[text()='Downloads at once (all)']]//span[contains(@class,'dls-default')]\", document, null, 9, null).singleNodeValue.textContent")
    check("the row says its default (4), has a Reset link, and says when it takes effect", default_text == "Default: 4"
          and page.locator('.dls .prefs-row:has-text("Downloads at once (all)") .dls-reset').count() == 1
          and "Takes effect within a few seconds" in page.locator('.dls .prefs-row:has-text("Downloads at once (all)") .dls-meta').inner_text())
    page.check("#dls-changed")
    page.wait_for_timeout(200)
    only = page.evaluate("Array.from(document.querySelectorAll('.dls .prefs-row')).filter((r) => !r.hidden && r.offsetParent !== null).map((r) => r.querySelector('.prefs-row__label').textContent)")
    check("'Changed from default' shows just that row", only == ["Downloads at once (all)"], only)
    page.click('.dls .prefs-row:has-text("Downloads at once (all)") .dls-reset')
    deadline = time.time() + 20
    while time.time() < deadline and Store().get_settings()["concurrency"]["total"] != 4:
        page.wait_for_timeout(200)
    deadline = time.time() + 10
    while time.time() < deadline and page.locator('.dls .prefs-row:visible').count() != 0:
        page.wait_for_timeout(150)
    check("Reset writes the hub's default (4) and the row leaves the changed list", Store().get_settings()["concurrency"]["total"] == 4
          and page.locator('.dls .prefs-row:visible').count() == 0, page.locator('.dls .prefs-row:visible').count())
    page.uncheck("#dls-changed")
    # the speed windows table
    page.click('.dls .prefs-group-table:has-text("Speed windows") >> text=Add a window')
    check("Add a window adds a table row with four boxes", page.locator('.dls .prefs-group-table:has-text("Speed windows") tbody tr').count() == 1
          and page.locator('.dls .prefs-group-table:has-text("Speed windows") tbody tr input, .dls .prefs-group-table:has-text("Speed windows") tbody tr select').count() == 4)
    page.click('.dls .prefs-group-table:has-text("Speed windows") >> text=Save')
    deadline = time.time() + 20
    while time.time() < deadline and not Store().get_settings().get("schedule"):
        page.wait_for_timeout(200)
    check("Save writes the whole list", len(Store().get_settings().get("schedule") or []) == 1, Store().get_settings().get("schedule"))
    page.fill('.dls .prefs-group-table:has-text("Speed windows") tbody tr input >> nth=1', "01:00")
    page.fill('.dls .prefs-group-table:has-text("Speed windows") tbody tr input >> nth=0', "01:00")
    page.dispatch_event('.dls .prefs-group-table:has-text("Speed windows") tbody tr input >> nth=0', "change")
    page.click('.dls .prefs-group-table:has-text("Speed windows") >> text=Save')
    page.wait_for_selector('.dls .prefs-group-table:has-text("Speed windows") tr.dls-table__err', timeout=20000)
    err = page.locator('.dls .prefs-group-table:has-text("Speed windows") tr.dls-table__err').inner_text()
    check("a refused list shows the hub's own sentence under the row it names", "schedule window 1" in err and "same time" in err, err)
    page.click('.dls .prefs-group-table:has-text("Speed windows") .dls-reset')
    deadline = time.time() + 20
    while time.time() < deadline and Store().get_settings().get("schedule"):
        page.wait_for_timeout(200)
    deadline = time.time() + 10
    while time.time() < deadline and page.locator('.dls .prefs-group-table:has-text("Speed windows") tbody tr').count() != 0:
        page.wait_for_timeout(150)
    check("Reset on the table writes the default (no windows) and empties it", not Store().get_settings().get("schedule")
          and page.locator('.dls .prefs-group-table:has-text("Speed windows") tbody tr').count() == 0,
          (Store().get_settings().get("schedule"), page.locator('.dls .prefs-group-table:has-text("Speed windows") tbody tr').count()))
    # rail jump
    page.click('.dls-rail__item:has-text("Safety")')
    page.wait_for_timeout(300)
    top = page.evaluate("[document.querySelector('#dls-safety').getBoundingClientRect().top, window.innerHeight]")
    check("a rail entry brings its section into view (the last one can't reach the top of the pane)", 0 <= top[0] < top[1], top)
    page.click("#skin-close")
    page.wait_for_selector("#prefs-backdrop", state="hidden", timeout=10000)


HIST_ROWS = 5000


def seed_history():
    """5,000 finished rows spread over 120 days, written straight into the temporary database (nothing runs them).  Every 20th
    failed, every 33rd was cancelled; the first 120 `done` rows point at a real file, the other `done` rows at one that is not there."""
    from jarvis.hub.store import Store

    st = Store()
    files = HOME / "hist-files"
    files.mkdir(exist_ok=True)
    now = time.time()
    rows = []
    for i in range(HIST_ROWS):
        state = "failed" if i % 20 == 7 else "cancelled" if i % 33 == 5 else "done"
        finished = now - 600 - i * (120 * 86400.0 / HIST_ROWS)
        dest = None
        if state == "done":
            dest = str(files / ("file%04d.bin" % i))
            if i < 120:
                Path(dest).write_bytes(b"x" * 10)
        rows.append(("hist%05d" % i, "File %04d.bin" % i, state, 1000 + (i * 7919) % 900000, finished - 5 - (i % 40), finished, finished, finished - 100,
                     dest, "HTTP 503 from the server. Tried 3 times." if state == "failed" else None))
    con = sqlite3.connect(str(st.path))
    con.executemany("INSERT INTO items(id, title, state, bytes_total, started_at, finished_at, updated_at, created_at, dest_file, error, engine, source_url) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?, 'http', 'http://example.invalid/x')", rows)
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM items WHERE state IN ('done','failed','cancelled')").fetchone()[0]
    failed = con.execute("SELECT COUNT(*) FROM items WHERE state='failed'").fetchone()[0]
    con.close()
    on_disk = sum(1 for r in rows[:120] if r[2] == "done")      # the files that exist
    return count, failed, on_disk


HIST_JS = """() => {
  const R = __hubRoot();
  const list = R.querySelector('#histList');
  return { rows: list.querySelectorAll('.hist__row').length, groups: Array.from(list.querySelectorAll('.hist__group')).map((g) => g.textContent),
           count: R.querySelector('#histCount').textContent, bulkHidden: R.querySelector('#histBulk').hidden, more: !R.querySelector('#histMore').hidden,
           chip: R.querySelector('#histFailedChip').hidden ? '' : R.querySelector('#histFailedChip').textContent };
}"""


def hist_state(page):
    return page.evaluate(HIST_JS)


def hist_wait(page, pred, ms=8000):
    deadline = time.time() + ms / 1000.0
    st = None
    while time.time() < deadline:
        st = hist_state(page)
        if pred(st):
            return True, st
        page.wait_for_timeout(100)
    return False, st


def section_history(page):
    print("\n-- N21: the History tab with %d generated rows --" % HIST_ROWS)
    total, failed, on_disk = seed_history()
    page.evaluate("""() => { window.__confirms = []; window.JarvisUI = window.JarvisUI || {}; window.JarvisUI.confirm = async (o) => { window.__confirms.push(o); return true; }; }""")
    t0 = time.time()
    page.evaluate("__hubRoot().querySelector('#tabHistory').click()")
    ok, st = hist_wait(page, lambda s: s["rows"] > 0)
    first_ms = (time.time() - t0) * 1000
    check("the first page is %d rows, not thousands, and it came back fast" % 100, ok and st["rows"] == 100 and first_ms < 5000, (st, round(first_ms)))
    check("the summary counts the WHOLE set, not the page (%d downloads, %d failed)" % (total, failed),
          st["count"].startswith("%d downloads" % total) and (" %d failed" % failed) in st["count"], st["count"])
    check("the days are grouped under headers, Today first", st["groups"][:1] == ["Today"] and len(st["groups"]) >= 2, st["groups"][:5])
    sticky = page.evaluate("getComputedStyle(__hubRoot().querySelector('.hist__group')).position")
    check("a day header is sticky", sticky == "sticky", sticky)
    check("the Failed chip shows the failed count", st["chip"] == "Failed %d" % failed, st["chip"])
    # scroll near the end a dozen times: each time the next 100 rows arrive, once each, and the page is not rebuilt
    t0 = time.time()
    last = st["rows"]
    for _ in range(12):
        page.evaluate("() => { const b = __hubRoot().querySelector('#histList'); b.scrollTop = b.scrollHeight; }")
        ok, st = hist_wait(page, lambda s: s["rows"] > last, ms=6000)
        if not ok:
            break
        last = st["rows"]
    scroll_s = time.time() - t0
    ids = page.evaluate("Array.from(__hubRoot().querySelectorAll('#histList .hist__row')).map((r) => r.getAttribute('data-id'))")
    check("scrolling loads the next pages, once each (%d rows after 12 scrolls, %.1f s)" % (len(ids), scroll_s), len(ids) == 1300 and len(set(ids)) == 1300 and st["more"], (len(ids), len(set(ids))))
    missing = page.evaluate("Array.from(__hubRoot().querySelectorAll('#histList .hist__row.is-missing')).length")
    expect_missing = page.evaluate("Array.from(__hubRoot().querySelectorAll('#histList .hist__row.state-done')).length") - sum(
        1 for i in page.evaluate("Array.from(__hubRoot().querySelectorAll('#histList .hist__row.state-done')).map((r) => r.getAttribute('data-id'))")
        if i.startswith("hist") and int(i[4:]) < 120)
    check("a finished row whose file is not there is marked 'file missing' (every page checks its own rows)", missing == expect_missing and missing > 1000, (missing, expect_missing))
    one = page.evaluate("""() => { const r = __hubRoot().querySelector('#histList .hist__row[data-id="hist00007"]'); return { reason: r.querySelector('.hist__reason') && r.querySelector('.hist__reason').textContent,
        retry: Array.from(r.querySelectorAll('.rbtn')).map((b) => b.textContent) }; }""")
    check("a failed row says why in one sentence and has a Retry button", one["reason"] == "HTTP 503 from the server." and "Retry" in one["retry"], one)
    # Failed chip: only the failed rows, and the count agrees
    page.evaluate("__hubRoot().querySelector('#histFailedChip').click()")
    ok, st = hist_wait(page, lambda s: s["count"].startswith("%d downloads" % failed))
    check("the Failed chip narrows the list to the failed rows and the summary follows", ok and st["rows"] == min(100, failed), st)
    for _ in range(6):
        page.evaluate("() => { const b = __hubRoot().querySelector('#histList'); b.scrollTop = b.scrollHeight; }")
        ok, st = hist_wait(page, lambda s: not s["more"], ms=3000)
        if ok:
            break
    check("the end of a list is reached: every failed row is there once, no Load more, the retention sentence closes it",
          ok and st["rows"] == failed and not st["more"] and page.evaluate("!!__hubRoot().querySelector('#histList .hist__note')"), st)
    page.evaluate("__hubRoot().querySelector('#histFailedChip').click()")
    hist_wait(page, lambda s: s["count"].startswith("%d downloads" % total))
    # sort: largest first
    page.evaluate("() => { const s = __hubRoot().querySelector('#histSort'); s.value = 'largest'; s.dispatchEvent(new Event('change')); }")
    ok, st = hist_wait(page, lambda s: s["groups"] == [] and s["rows"] > 0)
    sizes = page.evaluate("Array.from(__hubRoot().querySelectorAll('#histList .hist__row .hist__meta')).slice(0, 5).map((m) => m.textContent)")
    check("sorted by size the list is flat (no day headers) and starts with the biggest", ok and "880 KB" in sizes[0], sizes)
    page.evaluate("() => { const s = __hubRoot().querySelector('#histSort'); s.value = 'newest'; s.dispatchEvent(new Event('change')); }")
    hist_wait(page, lambda s: len(s["groups"]) >= 2)
    # select a range with shift, remove it: the confirmation says how many and that no file goes; the file stays
    page.evaluate("""() => { const c = __hubRoot().querySelectorAll('#histList .hist__check[data-id]'); c[2].click(); }""")
    page.evaluate("""() => { const c = __hubRoot().querySelectorAll('#histList .hist__check[data-id]');
        c[5].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true })); }""")
    st = hist_state(page)
    sel = page.evaluate("__hubRoot().querySelector('#histSelCount').textContent")
    check("Shift-click selects the range between two checkboxes and the bulk bar shows", not st["bulkHidden"] and sel == "4 selected", sel)
    gone = page.evaluate("Array.from(__hubRoot().querySelectorAll('#histList .hist__check:checked')).map((c) => c.getAttribute('data-id'))")
    page.evaluate("__hubRoot().querySelector('#histBulkRemove').click()")
    deadline = time.time() + 8
    removed_n = 0
    while time.time() < deadline:
        from jarvis.hub.store import Store
        removed_n = sum(1 for i in gone if Store().get_item(i) is None)
        if removed_n == len(gone):
            break
        page.wait_for_timeout(150)
    conf = page.evaluate("window.__confirms[window.__confirms.length - 1]")
    check("bulk Remove asked first, said how many, and said no file is deleted", conf and "4" in conf["title"] and "No downloaded file is deleted" in conf["body"], conf)
    check("bulk Remove removed exactly those rows", removed_n == len(gone) == 4, (removed_n, gone))
    kept = sum(1 for i in range(120) if (HOME / "hist-files" / ("file%04d.bin" % i)).exists())
    check("and every downloaded file is still on disk", kept == on_disk, (kept, on_disk))
    ok, st = hist_wait(page, lambda s: s["count"].startswith("%d downloads" % (total - 4)))
    check("the list and the summary follow the removal", ok, st and st["count"])
    # filters that match nothing: the empty state offers Clear filters
    page.evaluate("""() => { const i = __hubRoot().querySelector('#histSearch'); i.value = 'no such title zzz'; i.dispatchEvent(new Event('input')); }""")
    ok, st = hist_wait(page, lambda s: s["rows"] == 0 and s["count"].startswith("0 downloads"))
    check("nothing matching shows the filtered empty state with a Clear filters button", ok and page.evaluate("!!__hubRoot().querySelector('#histEmptyClear')"), st)
    page.evaluate("__hubRoot().querySelector('#histEmptyClear').click()")
    ok, st = hist_wait(page, lambda s: s["rows"] > 0)
    check("Clear filters brings the list back", ok, st)


def main():
    start_server()
    time.sleep(6)                       # the scheduler's startup tick autostarts daemons a moment after the server answers
    kill_daemons()
    time.sleep(1)
    ids = seed()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            skip("no browser engine could be started (%s); run `playwright install chromium`" % str(exc).splitlines()[0][:100])
        engine = "Chromium %s via Playwright" % browser.version
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_panel(page)
        section_reorder(page, ids)
        section_buttons(page)
        section_sources(page, ids)
        section_settings(page)
        section_history(page)
        check("no page error was raised in the whole run", not errors, errors[:3])
        browser.close()
    print("\nengine: %s; server: the real web/server.js on %s; back end: the real hub_admin and store in %s" % (engine, BASE, HOME))
    print("\n%d passed, %d failed" % (passed, failed))


try:
    main()
finally:
    kill_daemons()
    if server and server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:  # noqa: BLE001
            server.kill()
    if KEEP:
        print("temporary HOME kept: %s" % HOME)
    else:
        shutil.rmtree(str(HOME), ignore_errors=True)
sys.exit(1 if failed else 0)
