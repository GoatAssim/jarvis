"""Download Hub, N22 part a (brief 30): every download made with the Jarvis browser goes to the hub.

Covers (jarvis/hub/browser_intake.py and its three hooks in browser_tools.py):
  * fakes (no browser): the listener is put on every page (existing, later, pop-ups) and never on the
    context's "download" event (Playwright has none); a download becomes a row `browser:<id>` that ends
    done, with the file in the library folder named by hub/naming.py; a hostile name stays inside the
    library; two downloads of one name never share a file; a failed save is a failed row and leaves no
    `.part`; the browser closing mid-save fails the row exactly once; a broken hub store still saves the
    file and spools the report; the page's address (a signed link) is never stored; the provider is push
    mode with no pause / resume / cancel and no control tool; no cookie API is used anywhere in the module;
  * the result the model reads (`annotate`): untouched when nothing happened, says the file was handed to
    the hub, turns a refused "Download is starting" navigation into a success, says honestly when a save
    is still running, waits (pumps the page) until a save has landed;
  * REAL headless Chromium through Playwright, when it is installed here (the test says which engine ran,
    or that none did): a click on a Content-Disposition: attachment link, a pop-up (target=_blank), a
    window.open, a navigation straight to an attachment, a slow file (the tool returns only when it has
    landed), a download the server cuts off (a failed row), and closing the browser afterwards loses nothing.

No network (a local HTTP server on 127.0.0.1), no Windows, no real GOG.  Run: python tests/test_hub_browser_intake.py
"""
import ast
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-browser-intake-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import browser_tools, hub_client  # noqa: E402
from jarvis.hub import browser_intake as bi  # noqa: E402
from jarvis.hub import naming, paths  # noqa: E402
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


def fresh():
    """Forget what the module remembers between checks (rows stay: ids are unique)."""
    with bi._lock:
        bi._saving.clear()
        del bi._notes[:]
    hub_client.set_store_factory(None)


def rows():
    return Store().list_items(provider="browser", include_disabled=True, include_dismissed=True, limit=200)


def row(item_id):
    return Store().get_item(item_id)


# --------------------------------------------------------------------------- fakes
class FakeDownload:
    def __init__(self, name="report.pdf", data=b"%PDF-fake", url="https://files.example.com/get?token=SECRETTOKEN&sig=abc",
                 error=None, on_save=None):
        self.suggested_filename = name
        self.url = url
        self._data, self._error, self._on_save = data, error, on_save

    def save_as(self, path):
        if self._on_save:
            self._on_save()
            raise RuntimeError("Target page, context or browser has been closed")
        if self._error:
            Path(path).write_bytes(self._data[:3])        # a half-written file, like a cut transfer
            raise RuntimeError(self._error)
        Path(path).write_bytes(self._data)

    def failure(self):
        return self._error


class FakePage:
    def __init__(self, url="https://example.com/"):
        self.url = url
        self.handlers = {}
        self.pumps = []
        self.on_pump = None

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def wait_for_timeout(self, ms):
        self.pumps.append(ms)
        if self.on_pump:
            self.on_pump(len(self.pumps))


class FakeContext:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.handlers = {}

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)


# --------------------------------------------------------------------------- watching the browser
fresh()
first, later = FakePage(), FakePage()
ctx = FakeContext([first])
check("attach watches the context", bi.attach(ctx) is True)
check("an open page gets the download listener", first.handlers.get("download") == [bi._on_download])
check("the context is told to call us for every later page and when it closes",
      ctx.handlers.get("page") == [bi._watch_page] and ctx.handlers.get("close") == [bi._on_context_closed])
check("Playwright's context has no download event, so none is used", "download" not in ctx.handlers)
bi.attach(ctx)
check("attach twice does not subscribe twice", len(ctx.handlers["page"]) == 1 and len(first.handlers["download"]) == 1)
ctx.handlers["page"][0](later)                         # a pop-up / new tab appears
check("a page that appears later (pop-up, new tab) is watched", later.handlers.get("download") == [bi._on_download])
ctx.handlers["page"][0](later)
check("the same page is not watched twice", len(later.handlers["download"]) == 1)

# --------------------------------------------------------------------------- one download, start to finish
fresh()
bi._on_download(FakeDownload("report.pdf", b"%PDF-1.4 hello"))
found = [r for r in rows() if r["title"] == "report.pdf"]
check("a download is a row of the browser provider", len(found) == 1 and found[0]["id"].startswith("browser:b"))
item = found[0]
check("it ends done", item["state"] == "done")
dest = Path(item.get("dest_file") or item.get("dest") or "")
check("the file is in the library folder, in the Files folder", dest.parent == LIB / "Files" and dest.name == "report.pdf")
check("the file has the bytes the browser downloaded", dest.read_bytes() == b"%PDF-1.4 hello")
check("no .part is left behind", not list((LIB / "Files").glob("*.part")))
check("the row knows its size", item.get("bytes_total") == len(b"%PDF-1.4 hello") and item.get("bytes_done") == item.get("bytes_total"))
blob = json.dumps(item, default=str)
check("the page's address is never stored (a signed link can hold a token)", "SECRETTOKEN" not in blob and "sig=abc" not in blob)
check("only the host name is kept", "files.example.com" in blob)
notes = bi.take_notes()
check("the model gets one note about it", len(notes) == 1 and notes[0]["state"] == "done" and notes[0]["path"] == str(dest))
check("notes are given once", bi.take_notes() == [])

prov = Store().get_provider("browser")
check("the provider is registered, push mode, labelled Browser downloads",
      prov and prov["mode"] == "push" and prov["label"] == "Browser downloads")
check("it offers no Pause, Resume or Cancel and no control tool",
      not ({"pause", "resume", "cancel", "retry", "prioritize"} & set(prov["capabilities"])) and prov["controls"] == {})
check("it offers Open and Show in folder", set(prov["capabilities"]) == {"open", "reveal"})
check("its open roots are the library", str(LIB) in [str(r) for r in prov["open_roots"]])
check("the provider is not one of the hub's own engines", "browser" not in __import__("jarvis.hub.states", fromlist=["x"]).RESERVED_PROVIDERS)

# kinds, names, folders
fresh()
bi._on_download(FakeDownload("clip.MKV", b"v"))
bi._on_download(FakeDownload("song.mp3", b"a"))
check("a video goes to the Video folder", (LIB / "Video" / "clip.MKV").is_file())
check("an audio file goes to the Audio folder", (LIB / "Audio" / "song.mp3").is_file())
bi._on_download(FakeDownload("..\\..\\evil?.exe", b"x"))
bi._on_download(FakeDownload("../../../etc/passwd", b"x"))
bi._on_download(FakeDownload("CON.txt", b"x"))
bi._on_download(FakeDownload("", b"x"))
inside = all(naming.under(LIB, p) for p in LIB.rglob("*") if p.is_file())
check("hostile names stay inside the library folder", inside and not (Path(HOME) / "etc").exists())
check("every one of them is a finished row", all(r["state"] == "done" for r in rows() if r["title"] in
      ("evil_.exe", "passwd", "_CON.txt", "download")))
bi.take_notes()

fresh()
bi._on_download(FakeDownload("same.zip", b"1"))
bi._on_download(FakeDownload("same.zip", b"2"))
bi._on_download(FakeDownload("same.zip", b"3"))
check("two downloads of one name never share a file",
      sorted(p.name for p in (LIB / "Files").glob("same*")) == ["same (1).zip", "same (2).zip", "same.zip"])
check("each file keeps its own bytes", {p.read_bytes() for p in (LIB / "Files").glob("same*")} == {b"1", b"2", b"3"})
bi.take_notes()

# an owner's extension folder is respected like for every other hub download
Store().set_settings({"ext_folders": {".epub": "Books"}})
fresh()
bi._on_download(FakeDownload("novel.epub", b"e"))
check("the owner's extension folder applies", (LIB / "Books" / "novel.epub").is_file())
bi.take_notes()

# --------------------------------------------------------------------------- failures
fresh()
bi._on_download(FakeDownload("broken.iso", b"abcdef", error="net::ERR_CONNECTION_RESET"))
bad = [r for r in rows() if r["title"] == "broken.iso"]
check("a failed save is a failed row", len(bad) == 1 and bad[0]["state"] == "failed")
check("the failure says why, in one line", "ERR_CONNECTION_RESET" in (bad[0].get("error") or ""))
check("a failed save leaves no file and no .part", not list(LIB.rglob("broken*")))
notes = bi.take_notes()
check("the model is told it failed", len(notes) == 1 and notes[0]["state"] == "failed" and "ERR_CONNECTION_RESET" in notes[0]["error"])

fresh()


def browser_closes():
    bi._on_context_closed()


bi._on_download(FakeDownload("midway.bin", b"abc", on_save=browser_closes))
mid = [r for r in rows() if r["title"] == "midway.bin"]
check("a browser that closes mid-save leaves a failed row, not a row stuck downloading",
      len(mid) == 1 and mid[0]["state"] == "failed" and "closed" in (mid[0].get("error") or ""))
check("... exactly one note, not two", len([n for n in bi.take_notes() if n["title"] == "midway.bin"]) == 1)
check("... and nothing is left in the saving list", bi.saving_now() == [])
check("... and no .part is left", not list(LIB.rglob("midway*")))

# the finished file cannot be put in place (a scanner holds it): it is kept, never deleted, and the row says where
fresh()
real_replace = os.replace
bi.REPLACE_WAIT_S = 0.01
swaps = {"n": 0}


def flaky_replace(src, dst):
    swaps["n"] += 1
    if swaps["n"] < 3:
        raise PermissionError("held by another process")
    return real_replace(src, dst)


os.replace = flaky_replace
bi._on_download(FakeDownload("held.bin", b"held-bytes"))
check("a rename that fails twice is retried and the file lands", (LIB / "Files" / "held.bin").read_bytes() == b"held-bytes"
      and [r for r in rows() if r["title"] == "held.bin"][0]["state"] == "done")
swaps["n"] = -100
os.replace = lambda src, dst: (_ for _ in ()).throw(PermissionError("held by another process"))
bi._on_download(FakeDownload("stuck.bin", b"stuck-bytes"))
os.replace = real_replace
stuck = [r for r in rows() if r["title"] == "stuck.bin"][0]
kept = list((LIB / "Files").glob("stuck.bin.part"))
check("a rename that never works fails the row", stuck["state"] == "failed" and "could not be renamed" in stuck["error"])
check("... but the downloaded bytes are KEPT, and the row says where", len(kept) == 1 and kept[0].read_bytes() == b"stuck-bytes"
      and str(kept[0]) in stuck["error"])
kept[0].unlink()
bi.take_notes()

# a library folder that cannot be made is a failed row with a plain sentence
fresh()
blocker = Path(HOME) / "afile"
blocker.write_text("x")
Store().set_settings({"folders.file": "Files2"})
(LIB / "Files2").write_text("I am a file where the folder should be")
bi._on_download(FakeDownload("nowhere.bin", b"x"))
nw = [r for r in rows() if r["title"] == "nowhere.bin"]
check("a folder that can't be made fails the row and never raises", len(nw) == 1 and nw[0]["state"] == "failed")
(LIB / "Files2").unlink()
Store().set_settings({"folders.file": "Files"})
bi.take_notes()

# the hub's store is down: the file is still saved, the reports wait in the inbox
fresh()


class BrokenStore:
    def __getattr__(self, name):
        raise RuntimeError("database is locked")


hub_client.set_store_factory(lambda: BrokenStore())
raised = None
try:
    bi._on_download(FakeDownload("offline.bin", b"still saved"))
except Exception as exc:  # noqa: BLE001
    raised = exc
hub_client.set_store_factory(None)
check("a broken hub store never raises into the browser", raised is None)
saved = list((Path(HOME) / "Downloads" / "Jarvis").rglob("offline.bin"))
check("the file is still saved (in the default library, the settings could not be read)", len(saved) == 1 and saved[0].read_bytes() == b"still saved")
check("the reports are spooled for the daemon", len(list(paths.inbox_dir().glob("*.json"))) >= 1)
check("the hub applies them later and the row appears", hub_client.drain_inbox() >= 1
      and any(r["title"] == "offline.bin" and r["state"] == "done" for r in rows()))
bi.take_notes()

# --------------------------------------------------------------------------- what the model reads
fresh()
plain = {"ok": True, "url": "https://example.com/"}
check("a result with nothing to say comes back untouched", bi.annotate(plain, FakePage()) is plain)
check("a non-dict result is left alone", bi.annotate("text", FakePage()) == "text")
bi._on_download(FakeDownload("invoice.pdf", b"i"))
out = bi.annotate({"ok": True, "url": "https://example.com/"}, FakePage())
check("the result lists the download", out["ok"] is True and out["downloads"][0]["title"] == "invoice.pdf"
      and out["downloads"][0]["state"] == "done")
check("the sentence says the file was handed to the hub, with its id and path",
      "handed to the Download Hub" in out["download_note"] and out["downloads"][0]["id"] in out["download_note"]
      and out["downloads"][0]["path"] in out["download_note"])
check("the sentence says there is no pause or cancel", "no pause or cancel" in out["download_note"])
check("the sentence is plain text, no markup characters from the page", "<" not in out["download_note"])
check("it is told once", "downloads" not in bi.annotate({"ok": True}, FakePage()))

fresh()
bi._on_download(FakeDownload("direct.zip", b"z"))
nav = bi.annotate({"ok": False, "error": "Navigation failed: Page.goto: Download is starting\nCall log:\n - navigating"},
                  FakePage("https://example.com/files"))
check("a navigation Chromium refused because it was a download becomes a success",
      nav["ok"] is True and "error" not in nav and nav["url"] == "https://example.com/files" and nav["downloads"])
fresh()
other = bi.annotate({"ok": False, "error": "Navigation failed: net::ERR_NAME_NOT_RESOLVED"}, FakePage())
check("a real navigation failure stays a failure", other["ok"] is False and "downloads" not in other)
fresh()
bi._on_download(FakeDownload("late.zip", b"z"))
oops = bi.annotate({"ok": False, "error": "Click failed: timeout"}, FakePage())
check("a failure that has a download note keeps ok false (only a refused navigation is turned)", oops["ok"] is False and oops["downloads"])

# a save that is still running
fresh()
page = FakePage()
fake_item = hub_client.begin("browser", "bslow", title="huge.iso", kind="file", state="downloading")
bi._saving["bslow"] = {"item": fake_item, "title": "huge.iso", "part": None, "started": time.monotonic() - 5, "done": False}
check("settle gives up after its limit and says so", bi.settle(page, 0.3) is False and len(page.pumps) >= 1)
still = bi.annotate({"ok": True}, page, max_s=0.2)
check("a save still running is reported as such, with a way to continue",
      still["downloads"][0]["state"] == "downloading" and "still being downloaded" in still["download_note"]
      and "closing the browser first would lose it" in still["download_note"])
check("the row is not touched by the wait", row("browser:bslow")["state"] == "downloading")
bi._finish_failed("bslow", bi._saving["bslow"], "test cleanup")
bi.take_notes()


# settle really pumps the page until the save has landed
fresh()
page = FakePage()
fake_item = hub_client.begin("browser", "bwait", title="wait.bin", kind="file", state="downloading")
entry = {"item": fake_item, "title": "wait.bin", "part": None, "started": time.monotonic(), "done": False}
bi._saving["bwait"] = entry


def landing(count):
    if count == 3:
        entry["done"] = True
        bi._saving.pop("bwait", None)
        fake_item.done(path=None)


page.on_pump = landing
check("settle keeps pumping the page until the save has landed", bi.settle(page, 5) is True and len(page.pumps) == 3)
check("a click waits for the download to start first", bi.annotate({"ok": True}, FakePage(), grace_ms=500) == {"ok": True})
gp = FakePage()
bi.annotate({"ok": True}, gp, grace_ms=500)
check("... by pumping the page for the grace time", gp.pumps and gp.pumps[0] == 500)
broken_page = FakePage()
broken_page.wait_for_timeout = lambda ms: (_ for _ in ()).throw(RuntimeError("page closed"))
check("a page that is gone never raises", bi.annotate({"ok": True}, broken_page, grace_ms=500) == {"ok": True})
bi.take_notes()

# --------------------------------------------------------------------------- rules about the code itself
src = (ROOT / "jarvis-cli" / "jarvis" / "hub" / "browser_intake.py").read_text(encoding="utf-8")
tree = ast.parse(src)
names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
forbidden = {"cookies", "add_cookies", "clear_cookies", "storage_state", "set_extra_http_headers", "headers", "all_headers",
             "request", "route", "http", "requests", "urlopen", "httpx", "socket"}
check("the module touches no cookie, header or network API", not (forbidden & names))
check("... and builds no Cookie / Authorization header text", not any(s.lower().startswith(("cookie", "authorization")) for s in strings))
imports = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree)
           if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (getattr(n, "names", None) or [None])}
check("it does not import an engine", not any("engines" in str(i) for i in imports))
tools_src = (ROOT / "jarvis-cli" / "jarvis" / "browser_tools.py").read_text(encoding="utf-8")
check("browser_tools attaches the listener where the context is made",
      tools_src.index("browser_intake.attach(context)") > tools_src.index("launch_persistent_context")
      and tools_src.index("browser_intake.attach(context)") < tools_src.index("page = context.pages[0]"))
check("the confirmation list is untouched: nothing here adds a way to download without it",
      "browser_click" in (ROOT / "jarvis-cli" / "jarvis" / "tool_safety.py").read_text(encoding="utf-8"))
check("browser_tools only imports the new module inside guarded blocks (a bug there never breaks the browser)",
      tools_src.count("from .hub import browser_intake") == 2 and "def _hub_downloads" in tools_src
      and "import browser_intake" not in tools_src.split("def _ensure_page")[0])


# --------------------------------------------------------------------------- REAL headless Chromium
def real_browser_part():
    try:
        import http.server
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIPPED real-browser checks: the playwright package is not installed here (nothing used instead)")
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

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/page":
                body = (b'<html><body><a href="/files/guide.bin">Download the guide</a> '
                        b'<a target="_blank" href="/files/popup.bin">Open in new tab</a> '
                        b'<button onclick="window.open(\'/files/script.bin\')">Script window</button> '
                        b'<a href="/files/slow.bin">Download the slow one</a> '
                        b'<a href="/files/cut.bin">Download the cut one</a> '
                        b'<a href="/page2">Plain page link</a></body></html>')
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/page2":
                body = b"<html><body>nothing to download here</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/files/"):
                name = path.rsplit("/", 1)[1]
                size = 240_000
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                if name == "slow.bin":
                    for _ in range(6):
                        self.wfile.write(b"s" * (size // 6))
                        self.wfile.flush()
                        time.sleep(0.5)
                elif name == "cut.bin":
                    self.wfile.write(b"c" * 1000)
                    self.wfile.flush()
                    self.connection.close()           # fewer bytes than promised: a failed transfer
                else:
                    self.wfile.write((name[:1] * size).encode())
            else:
                self.send_response(404)
                self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    fresh()
    try:
        page, err = browser_tools._ensure_page()
        if err:
            print("SKIPPED real-browser checks: the Jarvis browser could not start here: %s" % err[:120])
            return
        agent = page.evaluate("navigator.userAgent")
        print("REAL BROWSER: %s (Playwright chromium, headless, through browser_tools._ensure_page)" % agent)
        got = browser_tools._local_tool_browser_goto({"url": base + "/page"})
        check("[chromium] a normal page loads and says nothing about downloads", got.get("ok") is True and "downloads" not in got)

        t0 = time.time()
        res = browser_tools._local_tool_browser_click({"description": "Download the guide"})
        check("[chromium] a click on an attachment link returns ok with the file handed to the hub",
              res.get("ok") is True and res.get("downloads") and res["downloads"][0]["state"] == "done")
        guide = LIB / "Files" / "guide.bin"
        check("[chromium] the file is in the library folder with all its bytes", guide.is_file() and guide.stat().st_size == 240_000)
        check("[chromium] the model's sentence names the hub and the path", "handed to the Download Hub" in res["download_note"]
              and str(guide) in res["download_note"])
        hub_row = row(res["downloads"][0]["id"])
        check("[chromium] it is a done row of the browser provider (Downloads and History)",
              hub_row and hub_row["state"] == "done" and hub_row["provider"] == "browser")
        check("[chromium] its .part is gone", not list((LIB / "Files").glob("*.part")))
        check("[chromium] the click returned quickly for a small file", time.time() - t0 < 15)

        res = browser_tools._local_tool_browser_click({"description": "Open in new tab"})
        check("[chromium] a link that opens a new tab (target=_blank) is caught",
              res.get("downloads") and (LIB / "Files" / "popup.bin").is_file())
        res = browser_tools._local_tool_browser_click({"description": "Script window"})
        check("[chromium] a window.open download is caught", res.get("downloads") and (LIB / "Files" / "script.bin").is_file())
        check("[chromium] all of them are rows in the hub", {r["title"] for r in rows() if r["state"] == "done"} >=
              {"guide.bin", "popup.bin", "script.bin"})

        res = browser_tools._local_tool_browser_goto({"url": base + "/files/direct.bin"})
        check("[chromium] a navigation straight to an attachment is a success with the file handed over",
              res.get("ok") is True and res.get("downloads") and (LIB / "Files" / "direct.bin").is_file())

        browser_tools._local_tool_browser_goto({"url": base + "/page"})
        t0 = time.time()
        res = browser_tools._local_tool_browser_click({"description": "Download the slow one"})
        took = time.time() - t0
        slow = LIB / "Files" / "slow.bin"
        check("[chromium] a slow download: the tool returns only after the file has landed",
              res.get("downloads") and res["downloads"][0]["state"] == "done" and slow.is_file() and slow.stat().st_size == 240_000)
        check("[chromium] ... which took about the 3 s the server needed (%.1f s)" % took, 2.0 <= took < 20)

        res = browser_tools._local_tool_browser_click({"description": "Download the cut one"})
        cut = [r for r in rows() if r["title"] == "cut.bin"]
        check("[chromium] a download the server cuts off is a failed row and a failed note",
              res.get("downloads") and res["downloads"][0]["state"] == "failed" and cut and cut[0]["state"] == "failed")
        check("[chromium] ... and leaves no file", not list(LIB.rglob("cut*")))

        res = browser_tools._local_tool_browser_click({"description": "Plain page link"})
        check("[chromium] an ordinary link adds no download note", res.get("ok") is True and "downloads" not in res)

        browser_tools.close_browser()
        check("[chromium] closing the browser loses nothing: every finished file is still there",
              all((LIB / "Files" / n).is_file() for n in ("guide.bin", "popup.bin", "script.bin", "direct.bin", "slow.bin")))
        check("[chromium] ... and no row is left downloading", not [r for r in rows() if r["state"] == "downloading"])
        check("[chromium] no browser download row has a control the hub could not honour",
              Store().get_provider("browser")["controls"] == {})
    finally:
        browser_tools.close_browser()
        server.shutdown()


real_browser_part()

print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
