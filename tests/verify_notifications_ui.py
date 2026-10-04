"""Browser verification for L.30 (notification rework): drives the REAL
web/public page in headless Chromium.

What is real: index.html, app.js, ui-kit.js, notifications.js/.css, and the
backend — every /api/notifications/* call is answered by shelling out to the
real `jarvis` CLI verbs (notify-history, notify-summary, notify-read,
notify-dismiss) exactly the way web/server.js does, against a throwaway HOME.

What is stubbed: everything else under /api (returns {}), and the websocket
(never connects). So this proves the page + module + CLI agree with each
other; it does not exercise server.js itself (no express in a bare checkout).

Not part of tests/run_tests.py (it only runs test_*.py): needs Playwright and
a Chromium.  Skips cleanly (exit 0, says so) when either is missing.

Run: python3 tests/verify_notifications_ui.py [--shots DIR]
"""

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis_l30ui_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "web" / "public"
sys.path.insert(0, str(ROOT / "jarvis-cli"))

try:
    from playwright.sync_api import sync_playwright
except Exception:  # noqa: BLE001
    print("skipped: playwright is not installed")
    sys.exit(0)

from jarvis import notifier  # noqa: E402

SHOTS = None
if "--shots" in sys.argv:
    SHOTS = Path(sys.argv[sys.argv.index("--shots") + 1])
    SHOTS.mkdir(parents=True, exist_ok=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# --- stub server -------------------------------------------------------------

def jarvis_cli(*args):
    env = dict(os.environ, HOME=HOME, USERPROFILE=HOME)
    out = subprocess.run([sys.executable, "-m", "jarvis.cli", *args], capture_output=True, text=True,
                         cwd=str(ROOT / "jarvis-cli"), env=env, timeout=60)
    return out.stdout


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(PUBLIC), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, text):
        body = (text or "{}").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/api/notifications/history":
            args = ["notify-history", q.get("limit", "200")]
            if q.get("unread") in ("1", "true"): args.append("--unread")
            if q.get("failed") in ("1", "true"): args.append("--failed")
            if q.get("needs_ack") in ("1", "true"): args.append("--needs-ack")
            if q.get("kind"): args.append("--kind=" + q["kind"])
            if q.get("source"): args.append("--source=" + q["source"])
            if q.get("q"): args.append("--q=" + q["q"])
            return self._raw(jarvis_cli(*args))
        if url.path == "/api/notifications/summary":
            return self._raw(jarvis_cli("notify-summary"))
        if url.path.startswith("/api/"):
            return self._json({})
        return super().do_GET()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        body = self._body()
        if url.path == "/api/notifications/read":
            if body.get("all") is True:
                return self._raw(jarvis_cli("notify-read", "all"))
            ids = [i for i in body.get("ids", []) if isinstance(i, str)]
            return self._raw(jarvis_cli("notify-read", ",".join(ids))) if ids else self._json({"read": 0})
        if url.path == "/api/notifications/dismiss":
            if body.get("scope") in ("read", "all"):
                return self._raw(jarvis_cli("notify-dismiss", body["scope"]))
            ids = [i for i in body.get("ids", []) if isinstance(i, str)]
            return self._raw(jarvis_cli("notify-dismiss", ",".join(ids))) if ids else self._json({"dismissed": 0})
        return self._json({})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --- seed data ---------------------------------------------------------------

def seed():
    def send(title, msg="", **kw):
        kw.setdefault("channels", ["inbox"])
        return notifier.notify(title, msg, **kw)

    older = send("Weekly report generated", "The weekly report is ready in Documents.", kind="task", level=2)
    old_read = send("Backup finished", "All 14 files copied.", kind="task", source="backup_job", level=2)
    failed = send("Backup failed", "Disk is full.", kind="task", source="backup_job", level=2, failed=True)
    for i in range(5):
        send("Clipboard changed", f"Copied text #{i}", kind="clipboard_watch", level=2)
    send("Stand-up in 5 minutes", "Daily meeting.", kind="reminder", level=2)
    send("Check the oven", "Persistent reminder", kind="reminder", level=3)
    send("Approve the deploy", "Needs a human to say yes.", kind="notify", level=5)
    send("Quiet note", "Silent, inbox only.", kind="notify", level=1)
    send("Long task output", "x" * 1500, kind="task", level=2)

    items = notifier._load_inbox()
    now = datetime.now()
    for rec in items:
        if rec["id"] == older["id"]:
            rec["created_at"] = (now - timedelta(days=3)).replace(microsecond=0).isoformat()
        elif rec["id"] in (old_read["id"], failed["id"]):
            rec["created_at"] = (now - timedelta(days=1)).replace(microsecond=0).isoformat()
    # one already read, to prove the Unread filter has something to exclude
    for rec in items:
        if rec["id"] == old_read["id"]:
            rec["read_at"] = now.replace(microsecond=0).isoformat()
    notifier._save_inbox(items)



def block_external(ctx, port):
    """Fonts and CDN libraries (marked, katex...) are not needed here and hang
    when there is no network; fail them fast so the page loads."""
    ctx.route("**/*", lambda route: route.continue_()
              if route.request.url.startswith(f"http://127.0.0.1:{port}") else route.abort())


# --- the checks --------------------------------------------------------------

def shot(page, name):
    if SHOTS:
        page.screenshot(path=str(SHOTS / f"{name}.png"))


def main():
    seed()
    srv = Server(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/index.html"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        block_external(ctx, port)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("localStorage.setItem('jarvis.onboarding.dismissed','1');")
        page.goto(base)
        page.wait_for_function("!!(window.JarvisNotifications && window.JarvisNotifications.open)")

        # ---- on load: the badge is the DURABLE unread count --------------------
        page.wait_for_function("document.getElementById('notifications-fab-badge') && !document.getElementById('notifications-fab-badge').hidden")
        expected_unread = notifier.summary()["unread"]
        check("badge shows the server's unread count after a load",
              page.inner_text("#notifications-fab-badge") == str(expected_unread),
              page.inner_text("#notifications-fab-badge"))

        # ---- on load: anything awaiting acknowledgment is back on screen -------
        page.wait_for_selector(".ntf")
        titles = page.eval_on_selector_all(".ntf .ntf__title", "els => els.map(e => e.textContent)")
        check("unacknowledged persistent items re-surface after a reload",
              "Check the oven" in titles and "Approve the deploy" in titles, titles)
        check("ordinary unread items do NOT pop up on load",
              "Stand-up in 5 minutes" not in titles, titles)
        check("those cards offer Acknowledge",
              page.locator(".ntf--ack .ntf__btn--primary").count() >= 1)
        shot(page, "01-reload-resurface")

        # ---- the bug being fixed: simultaneous notifications overwrote each other
        page.evaluate("document.querySelectorAll('.ntf').forEach(n => n.remove())")
        page.evaluate("""() => {
            const J = window.JarvisNotifications;
            J.show({id:'live-a', title:'First', message:'one', kind:'reminder', level:2, created_at:'2026-10-04T10:00:00'});
            J.show({id:'live-b', title:'Second', message:'two', kind:'task', level:2, created_at:'2026-10-04T10:00:01'});
            J.show({id:'live-c', title:'Third', message:'three', kind:'notify', level:2, created_at:'2026-10-04T10:00:02', failed:true});
        }""")
        n_cards = page.locator(".ntf").count()
        check("three notifications in one tick give three cards (the old toast kept only the last)", n_cards == 3, n_cards)
        page.evaluate("window.JarvisHost && window.JarvisHost.toast('An error elsewhere', 'error')")
        check("an unrelated error toast no longer wipes them", page.locator(".ntf").count() == 3)
        page.wait_for_timeout(350)          # the stack's step-down transition
        geo = page.evaluate("""() => ({toastBottom: document.getElementById('toast').getBoundingClientRect().bottom,
                                         stackTop: document.getElementById('ntf-stack').getBoundingClientRect().top})""")
        check("the cards step down below a visible error toast instead of hiding behind it",
              geo["stackTop"] >= geo["toastBottom"], geo)
        check("a failed one is marked as failed", page.locator(".ntf--failed").count() == 1)
        shot(page, "02-three-cards")

        # ---- coalescing + dedupe ---------------------------------------------------
        page.evaluate("document.querySelectorAll('.ntf').forEach(n => n.remove())")
        page.evaluate("window.JarvisNotifications.show({id:'dup-1', title:'Same', message:'a', kind:'clipboard_watch', level:2, created_at:'2026-10-04T10:00:00'})")
        page.evaluate("window.JarvisNotifications.show({id:'dup-2', title:'Same', message:'b', kind:'clipboard_watch', level:2, created_at:'2026-10-04T10:00:05'})")
        page.evaluate("window.JarvisNotifications.show({id:'dup-3', title:'Same', message:'c', kind:'clipboard_watch', level:2, created_at:'2026-10-04T10:00:09'})")
        check("the same notification repeated coalesces into one card", page.locator(".ntf").count() == 1)
        check("with a x3 count", page.inner_text(".ntf .ntf__count") == "\u00d73", page.inner_text(".ntf .ntf__count"))
        check("showing the latest body", "c" == page.inner_text(".ntf .ntf__body").strip())
        before = page.inner_text("#notifications-fab-badge")
        page.evaluate("window.JarvisNotifications.show({id:'dup-3', title:'Same', message:'c', kind:'clipboard_watch', level:2})")
        check("the same id arriving by a second path (stream + push) is ignored",
              page.inner_text(".ntf .ntf__count") == "\u00d73" and page.inner_text("#notifications-fab-badge") == before)

        # ---- cap ---------------------------------------------------------------------
        page.evaluate("document.querySelectorAll('.ntf').forEach(n => n.remove())")
        page.evaluate("""() => { for (let i = 0; i < 7; i++) window.JarvisNotifications.show(
            {id:'cap-'+i, title:'Distinct '+i, message:'m', kind:'k'+i, level:2, created_at:'2026-10-04T10:00:0'+i}); }""")
        page.wait_for_timeout(450)          # a removed card fades out for 180ms before it leaves the DOM
        check("the stack never shows more than four cards", page.locator(".ntf").count() <= 4, page.locator(".ntf").count())

        # ---- level 1 makes no card -------------------------------------------------
        page.evaluate("document.querySelectorAll('.ntf').forEach(n => n.remove())")
        page.evaluate("window.JarvisNotifications.show({id:'silent-1', title:'Quiet', message:'q', kind:'notify', level:1})")
        check("a level-1 (silent) notification raises no card", page.locator(".ntf").count() == 0)

        # ---- a toast card can be dismissed without losing the notification -----------
        page.evaluate("window.JarvisNotifications.show({id:'x-1', title:'Dismiss me', message:'m', kind:'notify', level:2})")
        page.click(".ntf__x")
        page.wait_for_function("document.querySelectorAll('.ntf').length === 0")
        check("the x hides the card", True)

        # ---- level 5 must not close another dialog --------------------------------------
        page.evaluate("""() => { window.__approval = 'pending';
            window.JarvisUI.confirm({title:'Allow run_shell?', confirmLabel:'Yes', cancelLabel:'No'})
              .then(v => { window.__approval = v ? 'approved' : 'denied'; }); }""")
        page.wait_for_selector(".jui-modal")
        page.evaluate("window.JarvisNotifications.show({id:'c5-1', title:'Confirm me', message:'needs ack', kind:'notify', level:5, persistent:true, confirm_required:true})")
        page.wait_for_timeout(600)
        check("a level-5 notification does not dismiss a pending approval dialog",
              page.evaluate("window.__approval") == "pending"
              and "Allow run_shell?" in page.inner_text(".jui-modal"))
        page.click(".jui-modal .jui-btn--primary")        # approve the tool
        check("the approval resolved normally", page.evaluate("window.__approval") in ("approved",))
        page.wait_for_function("document.querySelector('.jui-modal') && document.querySelector('.jui-modal').innerText.includes('Confirm me')", timeout=5000)
        check("then the notification's own Acknowledge dialog appears", True)
        page.click(".jui-modal .jui-btn--primary")        # Acknowledge
        page.wait_for_function("!document.querySelector('.jui-modal')")
        # the live record id isn't in the durable inbox, so verify via a real one below
        # ---- level 5 + a real record: Acknowledge is RECORDED ---------------------------
        real = notifier.notify("Real confirm", "persisted", channels=["inbox"], level=5)
        rec = real
        page.evaluate("(n) => window.JarvisNotifications.show(n)", {k: rec[k] for k in
                      ("id", "title", "message", "summary", "kind", "level", "persistent", "confirm_required", "created_at")})
        page.wait_for_selector(".jui-modal")
        page.click(".jui-modal .jui-btn--primary")
        page.wait_for_function("!document.querySelector('.jui-modal')")
        page.wait_for_timeout(900)
        now = {i["id"]: i for i in notifier.history(500)}
        check("clicking Acknowledge on a level-5 dialog is recorded (the old code threw it away)",
              bool(now[rec["id"]].get("acked_at")) and bool(now[rec["id"]].get("read_at")))

        # ---- panel ------------------------------------------------------------------------------
        page.evaluate("document.querySelectorAll('.ntf').forEach(n => n.remove())")
        page.click("#btn-notifications-fab")
        page.wait_for_selector(".nt-row")
        shot(page, "03-panel")
        status = page.inner_text("#notifications-status-line")
        check("the status line states unread and total", "unread" in status and "total" in status, status)
        sections = page.eval_on_selector_all(".nt-section", "els => els.map(e => e.textContent)")
        check("a pinned 'needs acknowledgment' section comes first", sections and sections[0].startswith("Needs your acknowledgment"), sections)
        check("rows are grouped by day", "Today" in sections and "Yesterday" in sections, sections)
        check("an older day gets a dated heading", any(s not in ("Today", "Yesterday") and not s.startswith("Needs") for s in sections), sections)
        counts = page.eval_on_selector_all(".nt-count", "els => els.map(e => e.textContent)")
        check("a burst of five collapses into one x5 row", "\u00d75" in counts, counts)
        check("failed rows say so in words", page.locator(".nt-badge--fail").count() >= 1)
        check("unread rows are marked", page.locator(".nt-row.is-unread").count() >= 3)
        check("the long message is cut with a Show more", page.locator(".nt-more").count() >= 1)
        page.locator(".nt-more").first.click()
        check("Show more reveals the full text", page.locator(".nt-body").evaluate_all("els => Math.max(...els.map(e => e.textContent.length))") >= 1500)

        # burst expand
        page.locator(".nt-count", has_text="\u00d75").click()
        check("the burst expands to its five individual entries", page.locator(".nt-burst__row").count() == 5, page.locator(".nt-burst__row").count())
        page.locator(".nt-count", has_text="\u00d75").click()

        # filters
        page.click("#notif-chips >> text=Failed")
        page.wait_for_function("document.querySelectorAll('.nt-row').length === 1")
        check("the Failed filter shows only the failed notification",
              "Backup failed" in page.inner_text(".nt-row .nt-title"))
        page.click("#notif-chips >> text=All")
        page.wait_for_function("document.querySelectorAll('.nt-row').length > 3")
        page.click("#notif-chips >> text=/^Unread/")
        page.wait_for_function("!Array.from(document.querySelectorAll('.nt-row')).some(r => r.textContent.includes('Backup finished'))")
        check("the Unread filter hides what you've read", True)
        page.click("#notif-chips >> text=All")
        page.fill("#notif-search", "backup")
        page.wait_for_function("document.querySelectorAll('.nt-row').length === 2")
        check("search narrows the list", page.locator(".nt-row").count() == 2)
        page.fill("#notif-search", "zzzzzz")
        page.wait_for_selector(".nt-empty")
        check("an empty result says nothing matches and offers to clear filters",
              "Nothing matches" in page.inner_text(".nt-empty") and page.locator(".nt-empty button").count() == 1)
        shot(page, "04-empty-filter")
        page.click(".nt-empty button")
        page.wait_for_selector(".nt-row")
        check("Clear filters restores the list and empties the search box", page.input_value("#notif-search") == "")
        page.select_option("#notif-kind", "reminder")
        page.wait_for_function("Array.from(document.querySelectorAll('.nt-row')).every(r => r.querySelector('.nt-badge').textContent === 'reminder')")
        check("the kind filter works", page.locator(".nt-row").count() >= 1)
        page.select_option("#notif-kind", "")
        page.wait_for_function("document.querySelectorAll('.nt-row').length > 3")

        # mark read by clicking a row
        row = page.locator(".nt-row.is-unread", has_text="Stand-up in 5 minutes")
        rid = row.get_attribute("data-id")
        row.click()
        page.wait_for_function("(id) => { const r = document.querySelector(`.nt-row[data-id='${id}']`); return r && !r.classList.contains('is-unread'); }", arg=rid)
        check("clicking a row marks it read", True)
        check("and the durable store agrees",
              bool({i['id']: i for i in notifier.history(500)}[rid].get('read_at')))

        # acknowledge a pinned item
        pinned_before = page.locator(".nt-row.is-ack").count()
        page.locator(".nt-row.is-ack .nt-btn--primary").first.click()
        page.wait_for_function("(n) => document.querySelectorAll('.nt-row.is-ack').length === n - 1", arg=pinned_before)
        check("Acknowledge on a pinned item removes it from the pinned section", True)
        check("and the store records the acknowledgment", len(notifier.summary()["needs_ack"]) == pinned_before - 1)

        # dismiss
        total_before = notifier.summary()["total"]
        page.locator(".nt-row", has_text="Weekly report generated").locator(".nt-btn--quiet").click()
        page.wait_for_function("!Array.from(document.querySelectorAll('.nt-row')).some(r => r.textContent.includes('Weekly report generated'))")
        check("Dismiss deletes one notification for good", notifier.summary()["total"] == total_before - 1)

        # keyboard
        page.keyboard.press("Escape")
        page.wait_for_function("document.getElementById('notifications-overlay').hidden")
        check("Escape closes the panel", True)
        page.click("#btn-notifications-fab")
        page.wait_for_selector(".nt-row")
        page.keyboard.press("/")
        check("/ focuses the search box", page.evaluate("document.activeElement.id") == "notif-search")
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_function("document.getElementById('notifications-overlay').hidden")
        page.click("#btn-notifications-fab")
        page.wait_for_selector(".nt-row")
        page.keyboard.press("j")
        check("j moves focus to the first row", page.evaluate("document.activeElement.classList.contains('nt-row')"))

        # mark all read / clear read
        page.click("#btn-notifications-markall")
        page.wait_for_function("document.getElementById('notifications-fab-badge').hidden")
        check("Mark all read zeroes the badge", True)
        check("and nothing awaits acknowledgment any more", notifier.summary()["needs_ack"] == [] and notifier.summary()["unread"] == 0)
        check("Mark all read disables itself", page.is_disabled("#btn-notifications-markall"))
        read_total = notifier.summary()["total"]
        page.click("#btn-notifications-clearread")
        page.wait_for_selector(".jui-modal")
        check("Clear read asks first and says how many", str(read_total) in page.inner_text(".jui-modal"), page.inner_text(".jui-modal"))
        page.click(".jui-modal .jui-btn >> text=Cancel")
        page.wait_for_function("!document.querySelector('.jui-modal')")
        check("cancelling deletes nothing", notifier.summary()["total"] == read_total)
        page.click("#btn-notifications-clearread")
        page.wait_for_selector(".jui-modal")
        page.click(".jui-modal .jui-btn >> text=Delete")
        page.wait_for_selector(".nt-empty")
        check("confirming deletes every read notification", notifier.summary()["total"] == 0)
        check("and the panel shows the empty state", "No notifications" in page.inner_text(".nt-empty"))
        shot(page, "05-empty")

        check("no uncaught page errors", not errors, errors)
        ctx.close()

        # ---- D-N1 resurfacing, with a fake clock --------------------------------------------------
        notifier._save_inbox([])
        rec = notifier.notify("Take the pills", "Persistent", channels=["inbox"], level=3)
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        block_external(ctx, port)
        page = ctx.new_page()
        errors2 = []
        page.on("pageerror", lambda e: errors2.append(str(e)))
        page.clock.install()
        page.goto(base)
        page.wait_for_function("!!(window.JarvisNotifications && window.JarvisNotifications.open)")
        page.clock.run_for(1500)
        page.wait_for_selector(".ntf")
        check("a persistent notification shows on load", page.locator(".ntf").count() == 1)
        page.clock.run_for(13000)
        check("and auto-hides after a while (D-N1: it re-surfaces, it does not stick)", page.locator(".ntf").count() == 0)
        page.clock.run_for(10 * 60 * 1000)
        page.wait_for_selector(".ntf", timeout=5000)
        check("it re-surfaces after 10 minutes if still unacknowledged", page.locator(".ntf").count() == 1)
        notifier.mark_read([rec["id"]])                        # acknowledged somewhere else (another tab / the panel)
        page.clock.run_for(13000)
        page.clock.run_for(10 * 60 * 1000)
        page.wait_for_timeout(600)
        check("once acknowledged elsewhere it stops re-surfacing", page.locator(".ntf").count() == 0)
        check("no uncaught page errors (clock run)", not errors2, errors2)
        ctx.close()
        browser.close()

    srv.shutdown()
    print(f"\n{len(PASS)} checks passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
