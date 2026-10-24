"""Download Hub, brief 6E: harden the browser-to-hub download hand-over (jarvis/hub/browser_intake.py and the
three hooks in jarvis/browser_tools.py).

Fakes (no browser):
  * problem 1, the hooks: browser_tools calls into the intake in three places; each is made to raise on
    purpose and the tool still answers (and still does its job once);
  * problem 3, "asked for": a download counts only when a click / goto is running on the page (or a page it
    opened) or just ended there; a script-started download is cancelled, saved nowhere, and reported in the
    NEXT result of any browser tool; pop-ups, two pages, the window, a tool that cannot cause a download;
  * problem 2, the worker: with no tool call running the browser's thread keeps pumping, so a save ends by
    itself; a browser that is gone fails the row with a plain reason.

REAL headless Chromium through Playwright when it is installed here (the test says which engine ran, or
that none did): the tool returns inside the bound with the row id and "still downloading" and the row
becomes done with no further call; a file that lands inside the bound is done in the same result; closing the
browser first fails the row; a download a page starts by itself is not saved and is reported; a download that
starts late (the server waits before its headers) is still saved; two pages, one clicked and one not; a
pop-up page that downloads; tools called from two threads (what the warm daemon does); the browser reopened
after a close still saves.

Kept: the confirmation on browser_click / browser_fill, no cookie / header API, the library folder and
hub/naming.py (never a temporary path), a failed download is a failed row.

No network (a local HTTP server on 127.0.0.1), no Windows, no real GOG.  Run: python tests/test_hub_browser_hardening.py
"""
import ast
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-browser-hardening-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import browser_tools, hub_client  # noqa: E402
from jarvis.hub import browser_intake as bi  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


LIB = Path(HOME) / "library"
Store().set_settings({"library_root": str(LIB)})


def rows():
    return Store().list_items(provider="browser", include_disabled=True, include_dismissed=True, limit=300)


def row_by_title(title):
    found = [r for r in rows() if r["title"] == title]
    return found[-1] if found else None


def fresh():
    with bi._lock:
        bi._saving.clear()
        del bi._notes[:]
        bi._live_pages[:] = []
        bi._parent.clear()
        bi._asked_until.clear()
        bi._active[0] = None
        bi._contexts.clear()
        bi._pages.clear()
    hub_client.set_store_factory(None)


# --------------------------------------------------------------------------- fakes
class FakeDownload:
    def __init__(self, name="report.pdf", data=b"%PDF-fake", url="https://files.example.com/get?token=SECRETTOKEN",
                 cancel_error=None):
        self.suggested_filename = name
        self.url = url
        self._data = data
        self._cancel_error = cancel_error
        self.cancelled = False
        self.saved = False

    def save_as(self, path):
        self.saved = True
        Path(path).write_bytes(self._data)

    def cancel(self):
        if self._cancel_error:
            raise RuntimeError(self._cancel_error)
        self.cancelled = True


class FakePage:
    def __init__(self, url="https://example.com/"):
        self.url = url
        self.handlers = {}
        self.pumps = 0
        self.on_pump = None
        self.gone = False

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def fire(self, event, *args):
        for fn in list(self.handlers.get(event, [])):
            fn(*args)

    def wait_for_timeout(self, ms):
        if self.gone:
            raise RuntimeError("Target page, context or browser has been closed")
        self.pumps += 1
        if self.on_pump:
            self.on_pump(self.pumps)
        time.sleep(min(ms, 20) / 1000.0)


class FakeContext:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.handlers = {}

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def fire(self, event, *args):
        for fn in list(self.handlers.get(event, [])):
            fn(*args)


def watched(*pages):
    """A fake context with these pages, watched by the intake."""
    ctx = FakeContext(list(pages))
    bi.attach(ctx)
    return ctx


def tool_that(fn):
    """A fake browser tool: runs `fn()` (which may fire download events) and answers ok."""
    def tool(args=None):
        fn()
        return {"ok": True}
    return tool


# --------------------------------------------------------------------------- the constants, and why
check("the bound for one tool call is short (under a quarter of the warm daemon's 30 s per call)",
      bi.SETTLE_BOUND_S + bi.SETTLE_GRACE_MS / 1000.0 <= browser_tools._DAEMON_CALL_TIMEOUT / 3.0 and bi.SETTLE_BOUND_S >= 2.0)
check("a click and a navigation can cause a download; typing, looking and waiting cannot",
      bi.CAUSAL_TOOLS == {"click", "goto"})
check("the 'asked for' window is longer than a 3 s countdown page and than the click's own wait",
      bi.ASKED_WINDOW_S > 3.0 and bi.ASKED_WINDOW_S * 1000 > bi.SETTLE_GRACE_MS)

# --------------------------------------------------------------------------- problem 1: the hooks never raise into a tool
print("--- hooks")


class FakePw:
    def __init__(self, ctx):
        self.chromium = self
        self._ctx = ctx

    def launch_persistent_context(self, **kwargs):
        return self._ctx

    def stop(self):
        pass


class FakeSync:
    def __init__(self, ctx):
        self._ctx = ctx

    def start(self):
        return FakePw(self._ctx)


class ClosableContext(FakeContext):
    def __init__(self, pages=()):
        super().__init__(pages)
        self.closed = 0

    def close(self):
        self.closed += 1

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


def clear_session():
    for key in ("page", "context", "playwright"):
        browser_tools._session[key] = None


saved_attach = bi.attach
orig_import = browser_tools._import_sync_playwright
ctx_a = ClosableContext([FakePage()])
browser_tools._import_sync_playwright = lambda: (lambda: FakeSync(ctx_a))
clear_session()


def boom(*args, **kwargs):
    raise RuntimeError("deliberate failure in the intake")


bi.attach = boom
page, err = browser_tools._ensure_page()
bi.attach = saved_attach
check("hook 1 (attach) raising: the browser still starts and hands back its page", page is ctx_a.pages[0] and err is None)
clear_session()
browser_tools._import_sync_playwright = orig_import

fake_page = FakePage()
fake_page.title = lambda: "Fake Title"
fake_page.goto = lambda url, **kw: None
orig_ensure = browser_tools._ensure_page
browser_tools._ensure_page = lambda: (fake_page, None)
calls = {"n": 0}
real_goto = fake_page.goto
fake_page.goto = lambda url, **kw: calls.__setitem__("n", calls["n"] + 1)


def goto():
    return browser_tools._local_tool_browser_goto({"url": "https://example.com/"})


base = goto()
check("a tool answers with its own result when nothing is wrong", base.get("ok") is True and base.get("title") == "Fake Title" and calls["n"] == 1)

for label, attr in (("run_tool", "run_tool"), ("the thread's ensure", None), ("_begin", "_begin"), ("_end", "_end"),
                    ("annotate", "annotate")):
    calls["n"] = 0
    if attr:
        saved = getattr(bi, attr)
        setattr(bi, attr, boom)
    else:
        saved = bi._owner.ensure
        bi._owner.ensure = boom
    try:
        out = goto()
    finally:
        if attr:
            setattr(bi, attr, saved)
        else:
            bi._owner.ensure = saved
    check("hook 2 (%s) raising: the tool still answers, and does its job exactly once" % label,
          out.get("ok") is True and out.get("title") == "Fake Title" and calls["n"] == 1)

calls["n"] = 0
saved_run = bi.run_tool
del bi.run_tool
out = goto()
bi.run_tool = saved_run
check("hook 2 with the intake module broken (no run_tool at all): the tool runs as it did before N22a", out.get("ok") is True and calls["n"] == 1)

calls["n"] = 0
bi._owner.ensure = lambda: False
out = goto()
bi._owner.ensure = _Owner_ensure = bi._Owner.ensure.__get__(bi._owner)
check("hook 2 with no thread to be had: the tool runs on the caller's thread", out.get("ok") is True and calls["n"] == 1)
browser_tools._ensure_page = orig_ensure

ctx_b = ClosableContext([FakePage()])
browser_tools._session["context"] = ctx_b
browser_tools._session["page"] = ctx_b.pages[0]
saved_closing = bi.closing
bi.closing = boom
browser_tools.close_browser()
bi.closing = saved_closing
check("hook 3 (closing) raising: the browser is still closed and the session cleared",
      ctx_b.closed == 1 and browser_tools._session["context"] is None and browser_tools._session["page"] is None)

# --------------------------------------------------------------------------- problem 3: asked for
print("--- asked for")
fresh()
A, B = FakePage("https://a.example/"), FakePage("https://b.example/")
ctx = watched(A, B)
page_of = lambda: A  # noqa: E731  the page the tools work on

# a click that downloads is saved
dl = FakeDownload("guide.bin", b"G" * 10)
out = bi.run_tool(tool_that(lambda: A.fire("download", dl)), "click", {}, page_of)
check("a click that downloads: the file is saved", dl.saved and not dl.cancelled and (LIB / "Files" / "guide.bin").read_bytes() == b"G" * 10)
check("... it is a done row and the result says so",
      row_by_title("guide.bin")["state"] == "done" and out["downloads"][0]["state"] == "done" and "handed to the Download Hub" in out["download_note"])

# a script-started download with no tool call running
fresh()
A, B = FakePage(), FakePage()
ctx = watched(A, B)
dl = FakeDownload("auto.bin", b"A")
A.fire("download", dl)
check("a download started by the page with no tool call running is cancelled", dl.cancelled and not dl.saved)
check("... nothing is saved and there is no row", not list(LIB.rglob("auto*")) and row_by_title("auto.bin") is None)
out = bi.run_tool(lambda args: {"ok": True}, "get_text", {}, lambda: A)
check("... the NEXT result of any tool (even one that cannot cause a download) says so",
      "tried to download 'auto.bin'; it was not saved" in out.get("download_note", "") and out["downloads"][0]["state"] == "refused")
check("... once", "download_note" not in bi.run_tool(lambda args: {"ok": True}, "get_text", {}, lambda: A))
check("... and the result is still ok", out["ok"] is True)

# the refusal text never carries the page's address or a path
fresh()
A = FakePage()
watched(A)
A.fire("download", FakeDownload("../../etc/pass" + "w" * 200 + ".txt", url="https://x.example/get?token=SECRETTOKEN"))
note = bi.take_notes()
check("a refused download's name is cleaned and capped, and the address is never kept",
      len(note) == 1 and len(note[0]["title"]) <= 80 and "/" not in note[0]["title"] and "SECRETTOKEN" not in repr(note))
fresh()
A = FakePage()
watched(A)
for _ in range(5):
    A.fire("download", FakeDownload("loop.bin"))
note = bi.take_notes()
check("a page that keeps trying is one note with a count, not five", len(note) == 1 and note[0]["times"] == 5)
check("... and the sentence says how many times", "(5 times)" in bi.sentence(note, []))
fresh()
A = FakePage()
watched(A)
raised = None
try:
    A.fire("download", FakeDownload("stuck.bin", cancel_error="already finished"))
except Exception as exc:  # noqa: BLE001
    raised = exc
check("a cancel that fails never raises into the browser (and is still reported)", raised is None and len(bi.take_notes()) == 1)

# a pop-up opened by a click that downloads is saved
fresh()
A, P = FakePage(), FakePage()
ctx = watched(A)
dl = FakeDownload("popfile.bin", b"P")


def click_opens_popup():
    ctx.fire("page", P)             # the context sees the new page
    A.fire("popup", P)              # and the opener says it opened it
    P.fire("download", dl)          # the pop-up downloads


bi.run_tool(tool_that(click_opens_popup), "click", {}, lambda: A)
check("a pop-up opened by the click that downloads is saved", dl.saved and (LIB / "Files" / "popfile.bin").is_file())

# the pop-up of a page that was NOT asked does not count
fresh()
A, B, P = FakePage(), FakePage(), FakePage()
ctx = watched(A, B)
ctx.fire("page", P)
B.fire("popup", P)
dl = FakeDownload("sneaky.bin")
bi.run_tool(tool_that(lambda: None), "click", {}, lambda: A)       # A was clicked, B's pop-up was not
P.fire("download", dl)
check("a pop-up opened by a page that was not clicked is refused", dl.cancelled and not dl.saved)

# two pages, one clicked and one not
fresh()
A, B = FakePage(), FakePage()
watched(A, B)
dlb, dla = FakeDownload("fromB.bin"), FakeDownload("fromA.bin", b"a")


def click_a_while_b_downloads():
    B.fire("download", dlb)          # the other page starts a download in the middle of the click
    A.fire("download", dla)


bi.run_tool(tool_that(click_a_while_b_downloads), "click", {}, lambda: A)
check("two pages: the clicked page's download is saved", dla.saved and (LIB / "Files" / "fromA.bin").is_file())
check("two pages: the other page's download, even in the middle of the click, is refused", dlb.cancelled and not dlb.saved)
dlb2 = FakeDownload("fromB2.bin")
B.fire("download", dlb2)
check("two pages: and so is the other page's download just after the click", dlb2.cancelled)

# the window after a click
fresh()
A = FakePage()
watched(A)
clock = [1000.0]
real_now = bi._now
bi._now = lambda: clock[0]
bi.run_tool(tool_that(lambda: None), "click", {}, lambda: A)
clock[0] += bi.ASKED_WINDOW_S - 0.5
late_ok = FakeDownload("late_ok.bin", b"l")
A.fire("download", late_ok)
clock[0] += 1.0
late_no = FakeDownload("late_no.bin")
A.fire("download", late_no)
bi._now = real_now
check("a download that starts inside the window after the click ends is saved", late_ok.saved)
check("a download that starts after the window is refused", late_no.cancelled and not late_no.saved)

# goto is a navigation: it can cause one too; fill / get_text / wait_for / screenshot cannot
for kind, expect_saved in (("goto", True), ("fill", False), ("get_text", False), ("wait_for", False), ("screenshot", False)):
    fresh()
    A = FakePage()
    watched(A)
    dl = FakeDownload("k_%s.bin" % kind)
    bi.run_tool(tool_that(lambda: A.fire("download", dl)), kind, {}, lambda: A)
    check("a '%s' call that sees a download: %s" % (kind, "saved" if expect_saved else "refused"),
          dl.saved == expect_saved and dl.cancelled == (not expect_saved))
fresh()
A = FakePage()
watched(A)
bi.run_tool(tool_that(lambda: None), "wait_for", {}, lambda: A)
dl = FakeDownload("after_wait.bin")
A.fire("download", dl)
check("a wait does not open a window: a download right after it is refused", dl.cancelled)

# the hub row of a saved one never holds the address; nothing of a refused one is in the hub
check("a refused download leaves no row of any kind", row_by_title("sneaky.bin") is None and row_by_title("fromB.bin") is None
      and row_by_title("late_no.bin") is None and row_by_title("after_wait.bin") is None)

# --------------------------------------------------------------------------- problem 2: the worker, with fakes
print("--- worker")
fresh()
W = FakePage()
watched(W)
item = hub_client.begin("browser", "bworker", title="worker.bin", kind="file", state="downloading")
entry = {"item": item, "title": "worker.bin", "part": None, "started": time.monotonic(), "done": False}
bi._saving["bworker"] = entry


def lands(count):
    if count == 4 and not entry["done"]:
        entry["done"] = True
        bi._saving.pop("bworker", None)
        item.done(path=None)


W.on_pump = lands
bi._owner.ensure()
deadline = time.time() + 5
while time.time() < deadline and not entry["done"]:
    time.sleep(0.05)          # no tool call is made: only the browser's thread is running
check("with no tool call running, the browser's thread keeps pumping and the save ends by itself", entry["done"])
check("... and the row is done", Store().get_item("browser:bworker")["state"] == "done")

fresh()
G = FakePage()
watched(G)
item = hub_client.begin("browser", "bgone", title="gone.bin", kind="file", state="downloading")
bi._saving["bgone"] = {"item": item, "title": "gone.bin", "part": None, "started": time.monotonic(), "done": False}
G.gone = True              # the browser is gone: every pump fails
deadline = time.time() + 5
while time.time() < deadline and Store().get_item("browser:bgone")["state"] == "downloading":
    time.sleep(0.05)
gone_row = Store().get_item("browser:bgone")
check("a browser that is gone fails the running row with a plain reason, by itself",
      gone_row["state"] == "failed" and "closed before the file was saved" in (gone_row.get("error") or ""))
check("... and the model is told on the next result", "closed before the file was saved" in
      (bi.run_tool(lambda a: {"ok": True}, "get_text", {}, lambda: None).get("download_note") or ""))

# closing(): waits for a save that is running when asked to, then fails what is left
fresh()
C = FakePage()
watched(C)
item = hub_client.begin("browser", "bclose", title="close.bin", kind="file", state="downloading")
bi._saving["bclose"] = {"item": item, "title": "close.bin", "part": None, "started": time.monotonic(), "done": False}
ran = []
t0 = time.time()
bi.closing(lambda: ran.append(1), 0.0)
check("closing with no wait runs the teardown at once and fails the running row",
      ran == [1] and time.time() - t0 < 2 and Store().get_item("browser:bclose")["state"] == "failed")
fresh()
C2 = FakePage()
watched(C2)
item = hub_client.begin("browser", "bwait2", title="wait2.bin", kind="file", state="downloading")
ent2 = {"item": item, "title": "wait2.bin", "part": None, "started": time.monotonic(), "done": False}
bi._saving["bwait2"] = ent2


def lands2(count):
    if count == 5 and not ent2["done"]:
        ent2["done"] = True
        bi._saving.pop("bwait2", None)
        item.done(path=None)


C2.on_pump = lands2
bi.closing(lambda: None, 10.0)
check("closing with a wait lets a save that is running finish first (then it is done, not failed)",
      Store().get_item("browser:bwait2")["state"] == "done")
bi.take_notes()

# --------------------------------------------------------------------------- kept rules
print("--- kept")
src = (ROOT / "jarvis-cli" / "jarvis" / "hub" / "browser_intake.py").read_text(encoding="utf-8")
tree = ast.parse(src)
names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
forbidden = {"cookies", "add_cookies", "clear_cookies", "storage_state", "set_extra_http_headers", "headers", "all_headers",
             "request", "route", "http", "requests", "urlopen", "httpx", "socket", "evaluate", "context_cookies"}
check("no cookie, header or network API is used in the intake (nothing is passed to another engine)", not (forbidden & names))
imports = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree)
           if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (getattr(n, "names", None) or [None])}
check("the intake imports no engine", not any("engines" in str(i) for i in imports))
safety = (ROOT / "jarvis-cli" / "jarvis" / "tool_safety.py").read_text(encoding="utf-8")
check("the confirmation on browser_click and browser_fill is still in tool_safety",
      'DEFAULT_CONFIRM_REQUIRED.add("browser_click")' in safety and 'DEFAULT_CONFIRM_REQUIRED.add("browser_fill")' in safety)
tools_src = (ROOT / "jarvis-cli" / "jarvis" / "browser_tools.py").read_text(encoding="utf-8")
check("no tool or confirmation was added to the browser tools (still seven local tools)",
      tools_src.count("@_via_intake(") == 7 and tools_src.count("def _local_tool_browser_") == 7)
check("no hub setting was added for this (settings.py does not know the intake)", "browser_intake" not in
      (ROOT / "jarvis-cli" / "jarvis" / "hub" / "settings.py").read_text(encoding="utf-8"))

# --------------------------------------------------------------------------- REAL headless Chromium
def real_browser_part():
    try:
        import http.server
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIPPED real-browser checks: the playwright package is not installed here (only the fakes above ran)")
        return
    try:
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        if not exe or not Path(exe).exists():
            raise RuntimeError("no Chromium binary at %s" % exe)
    except Exception as exc:  # noqa: BLE001
        print("SKIPPED real-browser checks: no Chromium for Playwright here (%s); only the fakes above ran" % str(exc)[:100])
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _html(self, body):
            body = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            if path == "/page":
                self._html('<html><body><a href="/files/small.bin">Small file</a> <a href="/files/slow.bin">Slow file</a> '
                           '<a href="/files/late.bin?delay=1.5">Late file</a> <a href="/files/cut.bin">Cut file</a> '
                           '<a target="_blank" href="/helper">Open helper</a> <a href="/count3">Countdown page</a> <a href="/page2">Plain link</a></body></html>')
            elif path == "/page2":
                self._html("<html><body>nothing to download here</body></html>")
            elif path == "/helper":                    # a page a click opens; it downloads by itself a moment later
                self._html("<html><body>helper<script>setTimeout(function(){location.href='/files/helper.bin'},300)</script></body></html>")
            elif path == "/count3":                    # "your download starts in 3 seconds" (started by the page's own timer)
                self._html("<html><body>starting in 3<script>setTimeout(function(){location.href='/files/countdown.bin'},3000)</script></body></html>")
            elif path == "/auto3":                     # a page that starts a download 3 s after it loaded
                self._html("<html><body>auto<script>setTimeout(function(){location.href='/files/auto.bin'},3000)</script></body></html>")
            elif path == "/auto1":                     # ... 1 s after
                self._html("<html><body>auto1<script>setTimeout(function(){location.href='/files/otherpage.bin'},1000)</script></body></html>")
            elif path.startswith("/files/"):
                name = path.rsplit("/", 1)[1]
                if "delay=" in query:
                    time.sleep(float(query.split("delay=")[1]))
                size = 240_000
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                try:
                    if name == "slow.bin":
                        for _ in range(6):
                            self.wfile.write(b"s" * (size // 6))
                            self.wfile.flush()
                            time.sleep(0.5)
                    elif name == "cut.bin":
                        self.wfile.write(b"c" * 1000)
                        self.wfile.flush()
                        self.connection.close()
                    else:
                        self.wfile.write((name[:1] * size).encode())
                except OSError:
                    pass
            else:
                self.send_response(404)
                self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = "http://127.0.0.1:%d" % server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fresh()
    saved_bound, saved_window = bi.SETTLE_BOUND_S, bi.ASKED_WINDOW_S
    try:
        page, err = bi.run_on_owner(browser_tools._ensure_page)
        if err:
            print("SKIPPED real-browser checks: the Jarvis browser could not start here: %s" % err[:120])
            return
        agent = bi.run_on_owner(page.evaluate, "navigator.userAgent")
        print("REAL BROWSER: %s (Playwright chromium, headless, through browser_tools._ensure_page)" % agent)
        go = lambda path: browser_tools._local_tool_browser_goto({"url": base + path})  # noqa: E731
        click = lambda what: browser_tools._local_tool_browser_click({"description": what})  # noqa: E731
        text = lambda: browser_tools._local_tool_browser_get_text({})  # noqa: E731

        # how long a download takes to START (the measurement the window is set from)
        # (the click tool spends about 1.5 s looking for the element first; the clock starts when the click action ENDS)
        starts, ends = [], []
        real_save, real_end = bi._save_download, bi._end

        def timed_save(download):
            starts.append(time.monotonic())
            return real_save(download)

        def timed_end(kind, page_of):
            ends.append(time.monotonic())
            return real_end(kind, page_of)

        bi._save_download, bi._end = timed_save, timed_end
        go("/page")
        res = click("Small file")
        small_start = starts[-1] - ends[-1] if starts else None
        late_res = click("Late file")
        late_end = ends[-1]
        deadline = time.time() + 8
        while len(starts) < 2 and time.time() < deadline:
            time.sleep(0.05)
        late_start = starts[-1] - late_end if len(starts) > 1 else None
        print("MEASURED: relative to the end of the click action, a link's download starts at %.2f s on the test page and at %.2f s "
              "when the server waits 1.5 s before its headers (about 0 or negative: Playwright's click waits for the navigation to begin)"
              % (small_start if small_start is not None else -99, late_start if late_start is not None else -99))
        check("[chromium] a link's download starts inside the click call, even from a slow server (%.2f s and %.2f s after it ends)"
              % (small_start if small_start is not None else -99, late_start if late_start is not None else -99),
              small_start is not None and late_start is not None and small_start < 0.5 and late_start < 0.5)
        check("[chromium] a file that lands inside the bound is done in the SAME result",
              res.get("ok") is True and res["downloads"][0]["state"] == "done" and (LIB / "Files" / "small.bin").stat().st_size == 240_000)
        check("[chromium] a file from a server that waits 1.5 s before its headers is saved and reported in the same result",
              (LIB / "Files" / "late.bin").is_file() and late_res.get("downloads") and late_res["downloads"][0]["state"] == "done")
        # a landing page whose own timer starts the download 3 s after load: the tool has returned by then
        go("/page")
        click("Countdown page")
        deadline = time.time() + 10
        while time.time() < deadline and not (LIB / "Files" / "countdown.bin").is_file():
            time.sleep(0.05)
        check("[chromium] a countdown page (download starts 3 s after load, after the tool returned) is saved: it is inside the window",
              (LIB / "Files" / "countdown.bin").is_file() and row_by_title("countdown.bin")["state"] == "done")
        count_text = text()
        check("[chromium] ... and the next tool result tells the model it was handed over",
              "countdown.bin" in count_text.get("download_note", "") and count_text["downloads"][0]["state"] == "done")
        bi._save_download = real_save          # (the end-of-click clock stays on: the bound check below uses it)

        # problem 2: a slow file, the bound, the worker
        bi.SETTLE_BOUND_S = 1.0
        go("/page")
        res = click("Slow file")
        took = time.monotonic() - ends[-1]
        still = res.get("downloads") or [{}]
        check("[chromium] a slow file (3 s): the tool returns inside the bound (%.1f s after the click; start wait %.1f s + bound %.1f s)"
              % (took, bi.SETTLE_GRACE_MS / 1000.0, bi.SETTLE_BOUND_S),
              took < bi.SETTLE_GRACE_MS / 1000.0 + bi.SETTLE_BOUND_S + 0.6)
        check("[chromium] ... saying 'still downloading', with the hub row id",
              res.get("ok") is True and still[0].get("state") == "downloading" and "still being downloaded" in res.get("download_note", "")
              and still[0].get("id", "").startswith("browser:b"))
        hub_row = Store().get_item(still[0]["id"])
        check("[chromium] ... and the row is 'downloading' in the hub at that moment", hub_row and hub_row["state"] == "downloading")
        deadline = time.time() + 15
        while time.time() < deadline and Store().get_item(still[0]["id"])["state"] == "downloading":
            time.sleep(0.1)                     # NO tool call is made: only the browser's thread works
        done_row = Store().get_item(still[0]["id"])
        slow = LIB / "Files" / "slow.bin"
        check("[chromium] the row becomes done by itself, with no further tool call", done_row["state"] == "done")
        check("[chromium] ... the file is in the library with every byte, and no .part is left",
              slow.is_file() and slow.stat().st_size == 240_000 and not list((LIB / "Files").glob("*.part")))
        follow = text()
        check("[chromium] ... and the next result tells the model where it went",
              "handed to the Download Hub" in follow.get("download_note", "") and str(slow) in follow["download_note"])
        check("[chromium] the file is named by the hub and kept in the library, never in a temporary path",
              str(slow).startswith(str(LIB)) and "playwright-artifacts" not in str(slow))

        # closing first
        bi.SETTLE_BOUND_S = 0.5
        go("/page")
        res = click("Slow file")
        rid = (res.get("downloads") or [{}])[0].get("id", "")
        closed = browser_tools._local_tool_browser_close({})
        failed_row = Store().get_item(rid) if rid else None
        check("[chromium] closing the browser while a file is still downloading: the row is failed, with a plain reason",
              failed_row and failed_row["state"] == "failed" and "closed before the file was saved" in (failed_row.get("error") or ""))
        check("[chromium] ... the close result says so, and no .part is left",
              closed.get("ok") is True and "closed before the file was saved" in closed.get("download_note", "")
              and not list((LIB / "Files").glob("*.part")))
        check("[chromium] ... and no row is left downloading", not [r for r in rows() if r["state"] == "downloading"])

        # the browser opens again after a close and still saves
        bi.SETTLE_BOUND_S = 8.0
        go("/page")
        res = click("Small file")
        check("[chromium] the browser reopened after a close still hands a download to the hub",
              res.get("downloads") and res["downloads"][0]["state"] == "done" and len([r for r in rows() if r["title"] == "small.bin"]) == 2)

        # problem 3 in a real browser: a page starts a download by itself
        bi.ASKED_WINDOW_S = 1.0
        go("/auto3")                                         # downloads 3 s later, nobody asked
        time.sleep(4.5)
        res = text()
        check("[chromium] a download a page starts by itself (3 s after load, no tool running) is not saved",
              not list(LIB.rglob("auto.bin")) and row_by_title("auto.bin") is None)
        check("[chromium] ... and the next result says 'a page tried to download auto.bin; it was not saved'",
              "tried to download 'auto.bin'; it was not saved" in res.get("download_note", ""))
        check("[chromium] ... told once", "download_note" not in text())

        # a pop-up page that a click opens and that downloads by itself 0.3 s later is saved
        bi.ASKED_WINDOW_S = 5.0
        go("/page")
        click("Open helper")
        deadline = time.time() + 8
        while time.time() < deadline and not (LIB / "Files" / "helper.bin").is_file():
            time.sleep(0.05)
        check("[chromium] a pop-up page opened by a click that downloads by itself is saved", (LIB / "Files" / "helper.bin").is_file())
        bi.take_notes()

        # two pages, one clicked and one not
        bi.ASKED_WINDOW_S = 5.0
        bi.SETTLE_BOUND_S = 1.0
        go("/page")
        context = browser_tools._session["context"]
        other = bi.run_on_owner(context.new_page)
        bi.run_on_owner(other.goto, base + "/auto1")        # this page downloads 1 s from now
        res = click("Slow file")                            # meanwhile the clicked page downloads a slow file
        time.sleep(4.0)
        check("[chromium] two pages: the clicked page's slow download is saved", (LIB / "Files" / "slow (1).bin").is_file()
              or len(list((LIB / "Files").glob("slow*.bin"))) >= 2)
        check("[chromium] two pages: the page that was not clicked started a download by itself and it is not saved",
              not list(LIB.rglob("otherpage*")) and row_by_title("otherpage.bin") is None)
        note = text().get("download_note", "") + res.get("download_note", "")
        check("[chromium] ... and the model is told about it", "tried to download 'otherpage.bin'" in note)
        bi.run_on_owner(other.close)
        bi.take_notes()
        bi.SETTLE_BOUND_S = 8.0

        # a failed download is a failed row
        go("/page")
        res = click("Cut file")
        cut = row_by_title("cut.bin")
        check("[chromium] a download the server cuts off is a failed row and a failed note",
              res.get("downloads") and res["downloads"][0]["state"] == "failed" and cut and cut["state"] == "failed")
        check("[chromium] ... and leaves no file", not list(LIB.rglob("cut*")))

        # tools called from two different threads (the warm daemon serves each request on a new thread)
        answers = {}

        def from_thread(name, fn):
            answers[name] = fn()

        for name, fn in (("first", lambda: go("/page2")), ("second", text)):
            t = threading.Thread(target=from_thread, args=(name, fn))
            t.start()
            t.join(30)
        check("[chromium] tools called from two different threads both work (Playwright belongs to one thread)",
              answers.get("first", {}).get("ok") is True and answers.get("second", {}).get("ok") is True)

        browser_tools.close_browser()
        check("[chromium] closing the browser at the end loses nothing: every finished file is still there",
              all((LIB / "Files" / n).is_file() for n in ("small.bin", "late.bin", "slow.bin", "helper.bin", "countdown.bin")))
        check("[chromium] no row is left downloading", not [r for r in rows() if r["state"] == "downloading"])
        check("[chromium] the browser provider still has no controls", Store().get_provider("browser")["controls"] == {})
    finally:
        bi.SETTLE_BOUND_S, bi.ASKED_WINDOW_S = saved_bound, saved_window
        bi._save_download, bi._end = real_save, real_end
        try:
            browser_tools.close_browser()
        except Exception:  # noqa: BLE001
            pass
        server.shutdown()


real_browser_part()

print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
