"""Browser verification for L.31 (MCP servers rework): drives the REAL
web/public page in headless Chromium.

What is real: index.html, app.js, ui-kit.js, mcp-servers.js/.css, and the
backend - every /api/mcp* call is answered by shelling out to the real `jarvis`
CLI verbs (mcp-status, mcp-refresh, mcp-edit) exactly the way web/server.js
does, against a throwaway HOME, with a REAL stdio MCP server process behind
"Refresh".

What is stubbed: everything else under /api (returns {}), and the websocket.
This proves page + module + CLI agree; it does not exercise server.js itself
(no express in a bare checkout - its routes are only syntax-checked).

Not part of run_tests.py (it only globs test_*.py): needs Playwright and a
Chromium.  Skips cleanly (exit 0, says so) when either is missing.

Run: python3 tests/verify_mcp_servers_ui.py [--shots DIR]
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
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis_l31ui_")
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

SHOTS = None
if "--shots" in sys.argv:
    SHOTS = Path(sys.argv[sys.argv.index("--shots") + 1])
    SHOTS.mkdir(parents=True, exist_ok=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


FAKE_SERVER = r'''
import json, sys
def send(o): sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); mid = m.get("id"); method = m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "ping", "description": "Ping the thing <b>bold</b>", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {}}}]}})
'''
SERVER_PY = Path(HOME) / "fake_mcp_server.py"
SERVER_PY.write_text(FAKE_SERVER, encoding="utf-8")


def jarvis_cli(*args):
    env = dict(os.environ, HOME=HOME, USERPROFILE=HOME)
    out = subprocess.run([sys.executable, "-m", "jarvis.cli", *args], capture_output=True, text=True,
                         cwd=str(ROOT / "jarvis-cli"), env=env, timeout=90)
    return out.stdout, out.returncode


CALLS = []   # every request the page made to /api/mcp*, for assertions


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(PUBLIC), **kw)

    def log_message(self, *a):
        pass

    def _send(self, body, status):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _cli(self, *args):
        # same contract as server.js parseJarvisJSON: error key -> 400
        out, _code = jarvis_cli(*args)
        try:
            status = 400 if json.loads(out).get("error") else 200
        except json.JSONDecodeError:
            status = 500
        return self._send(out or "{}", status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/mcp":
            CALLS.append(("GET", url.path))
            return self._cli("mcp-status")
        if url.path.startswith("/api/"):
            return self._send("{}", 200)
        return super().do_GET()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        body = self._body()
        parts = url.path.strip("/").split("/")
        if url.path == "/api/mcp/refresh":
            CALLS.append(("POST", url.path, body))
            server = body.get("server")
            return self._cli("mcp-refresh", server) if server else self._cli("mcp-refresh")
        if url.path == "/api/mcp/server":
            CALLS.append(("POST", url.path, body))
            args = ["mcp-edit", "save", body.get("name", ""), json.dumps(body.get("spec", {}))]
            if body.get("replace"):
                args += ["--replace", body["replace"]]
            return self._cli(*args)
        if len(parts) == 5 and parts[:3] == ["api", "mcp", "server"]:
            name, action = urllib.parse.unquote(parts[3]), parts[4]
            CALLS.append(("POST", url.path))
            return self._cli("mcp-edit", action, name)
        return self._send("{}", 200)

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if len(parts) == 4 and parts[:3] == ["api", "mcp", "server"]:
            CALLS.append(("DELETE", url.path))
            return self._cli("mcp-edit", "remove", urllib.parse.unquote(parts[3]))
        return self._send("{}", 200)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def block_external(ctx, port):
    ctx.route("**/*", lambda route: route.continue_()
              if route.request.url.startswith(f"http://127.0.0.1:{port}") else route.abort())


def shot(page, name):
    if SHOTS:
        page.screenshot(path=str(SHOTS / f"{name}.png"))


def config():
    p = Path(HOME) / ".jarvis" / "mcp_config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"servers": {}}


def cards(page):
    return page.eval_on_selector_all(".mcp-card .mcp-card__name", "els => els.map(e => e.textContent)")


def card(page, name):
    return page.locator(f'.mcp-card[data-server="{name}"]')


def settle(page):
    page.wait_for_function("!document.querySelector('.mcp-card.is-busy')", timeout=60000)


def main():
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
        page.wait_for_function("!!(window.JarvisMcp && window.JarvisMcp.open)")

        # ---- opening it: through the real Menu item --------------------------
        page.click("#btn-panel-menu") if page.locator("#btn-panel-menu").count() else None
        opened_by_menu = False
        for sel in ("#menu-item-mcp",):
            try:
                if page.locator("#btn-panel-menu").count() == 0:
                    raise RuntimeError("no menu trigger id")
                page.locator(sel).click(timeout=2000)
                opened_by_menu = not page.locator("#mcp-overlay").get_attribute("hidden")
            except Exception:  # noqa: BLE001
                pass
        if not opened_by_menu:
            page.evaluate("window.JarvisHost.openPanel('mcp')")   # the slash verb /mcp uses this
        page.wait_for_selector("#mcp-overlay:not([hidden])")
        page.wait_for_selector(".mcp-empty__title")
        check("empty state explains MCP and offers Add (no 'edit the JSON by hand')",
              "No MCP servers yet" in page.inner_text(".mcp-empty__title")
              and "mcp_config.json" not in page.inner_text(".mcp-empty")
              and page.locator(".mcp-empty .btn--primary").count() == 1)
        check("status line says so", page.inner_text("#mcp-status-line") == "no servers yet", page.inner_text("#mcp-status-line"))
        check("Refresh all is disabled with nothing switched on", page.is_disabled("#btn-mcp-refresh"))
        shot(page, "01-empty")

        # ---- adding a local server through the form ---------------------------
        page.click("#btn-mcp-add")
        page.wait_for_selector("#mcp-editor:not([hidden])")
        check("the list view is hidden while the editor is open", page.locator("#mcp-list-view").is_hidden())
        page.click("#mcp-f-save")
        check("saving an empty form is refused in the form, with no request sent",
              "name" in page.inner_text(".mcp-editor__error").lower() and not any(c[1] == "/api/mcp/server" for c in CALLS),
              page.inner_text(".mcp-editor__error"))
        page.fill("#mcp-f-name", "Live One")
        page.fill("#mcp-f-command", sys.executable)
        page.fill("#mcp-f-args", str(SERVER_PY))
        page.click("text=+ Add variable")
        page.fill(".mcp-envrow .mcp-input--key", "API_KEY")
        page.fill(".mcp-envrow input[type=password]", "topsecret-123")
        page.fill("#mcp-f-desc", "A <i>fake</i> server")
        check("Switched on is ticked by default for a new server", page.is_checked("#mcp-f-enabled"))
        check("Trusted is NOT ticked by default", not page.is_checked("#mcp-f-trusted"))
        shot(page, "02-editor")
        page.click("#mcp-f-save")
        page.wait_for_selector('.mcp-card[data-server="Live One"]', timeout=60000)
        settle(page)
        check("the server appears as a card", cards(page) == ["Live One"], cards(page))
        saved = config()["servers"]["Live One"]
        check("the real config file has it, switched on, confirm-gated, with the env value",
              saved["enabled"] is True and saved["trusted"] is False and saved["env"] == {"API_KEY": "topsecret-123"}
              and saved["args"] == [str(SERVER_PY)] and saved["description"] == "A <i>fake</i> server", saved)
        check("saving a switched-on server connected to it right away (real tools/list)",
              card(page, "Live One").locator(".mcp-card__state").inner_text().strip().lower() == "ready"
              and "2 tools" in card(page, "Live One").inner_text(), card(page, "Live One").inner_text())
        check("the editor closed", page.locator("#mcp-editor").is_hidden())
        shot(page, "03-ready")

        # ---- the secret never reaches the page ---------------------------------
        html = page.content()
        check("an environment VALUE is nowhere in the page", "topsecret-123" not in html)
        card(page, "Live One").get_by_text("Details").click()
        check("Details lists the variable NAME, never its value",
              "API_KEY" in card(page, "Live One").inner_text() and "topsecret" not in card(page, "Live One").inner_text())

        # ---- text, not markup --------------------------------------------------
        card(page, "Live One").get_by_text("Tools (2)").click()
        check("a tool description containing <b> is shown as text, not rendered",
              page.locator(".mcp-tool__desc b").count() == 0 and "<b>bold</b>" in page.inner_text(".mcp-tools"))
        check("a description containing <i> is shown as text too",
              page.locator(".mcp-card__desc i").count() == 0 and "<i>fake</i>" in card(page, "Live One").inner_text())
        check("each tool shows the full name the model will see", "mcp_live_one_ping" in page.inner_text(".mcp-tools"))

        # ---- the switch --------------------------------------------------------
        card(page, "Live One").locator(".mcp-switch").click()
        settle(page)
        check("switching off flips the card to Off", card(page, "Live One").locator(".mcp-card__state").inner_text().strip().lower() == "off")
        check("...and the config says enabled false", config()["servers"]["Live One"]["enabled"] is False)
        check("...and the cached tools are gone at once", not (Path(HOME) / ".jarvis" / "mcp_cache.json").exists()
              or "live_one" not in json.loads((Path(HOME) / ".jarvis" / "mcp_cache.json").read_text()))
        check("the switch's aria-checked follows", card(page, "Live One").locator(".mcp-switch").get_attribute("aria-checked") == "false")
        before = len([c for c in CALLS if c[1] == "/api/mcp/refresh"])
        card(page, "Live One").locator(".mcp-switch").click()
        settle(page)
        check("switching back on connects again by itself (one refresh for that server only)",
              len([c for c in CALLS if c[1] == "/api/mcp/refresh"]) == before + 1
              and [c for c in CALLS if c[1] == "/api/mcp/refresh"][-1][2] == {"server": "live_one"}
              and "ready" in card(page, "Live One").inner_text().lower(), CALLS[-3:])

        # ---- trust asks first --------------------------------------------------
        card(page, "Live One").locator(".mcp-trust input").click()
        page.wait_for_selector(".jui-modal")
        page.wait_for_function("document.activeElement && document.activeElement.textContent === 'Keep asking'", timeout=3000)
        check("turning Trusted on asks for confirmation, defaulting to Keep asking",
              "Trust Live One" in page.inner_text(".jui-modal__title"), page.inner_text(".jui-modal__title"))
        page.click(".jui-modal >> text=Keep asking")
        page.wait_for_selector(".jui-modal", state="detached")
        check("declining changes nothing", config()["servers"]["Live One"]["trusted"] is False)
        card(page, "Live One").locator(".mcp-trust input").click()
        page.wait_for_selector(".jui-modal")
        page.click(".jui-modal >> text=Trust it")
        settle(page)
        page.wait_for_function("document.querySelector('.mcp-card .mcp-trust input').checked")
        check("confirming trusts it", config()["servers"]["Live One"]["trusted"] is True)
        check("the card says trusted in words", "trusted" in card(page, "Live One").locator(".mcp-card__flags").inner_text().lower())
        card(page, "Live One").locator(".mcp-trust input").click()
        settle(page)
        page.wait_for_function("!document.querySelector('.mcp-card .mcp-trust input').checked")
        check("un-trusting needs no confirmation", config()["servers"]["Live One"]["trusted"] is False)

        # ---- editing: secrets kept, one field changed ---------------------------
        card(page, "Live One").get_by_text("Edit").click()
        page.wait_for_selector("#mcp-editor:not([hidden])")
        check("the editor shows the stored variable NAME with an 'unchanged' placeholder, never the value",
              page.input_value(".mcp-envrow .mcp-input--key") == "API_KEY"
              and "unchanged" in page.get_attribute(".mcp-envrow input[type=password]", "placeholder")
              and page.input_value(".mcp-envrow input[type=password]") == "")
        check("arguments are shown one per line", page.input_value("#mcp-f-args") == str(SERVER_PY))
        page.fill("#mcp-f-desc", "renamed description")
        # a reload happening in the background must not wipe what is being typed
        page.evaluate("window.JarvisMcp.refresh()")
        page.wait_for_timeout(1200)
        check("a background reload doesn't wipe the open form", page.input_value("#mcp-f-desc") == "renamed description")
        page.click("#mcp-f-save")
        page.wait_for_selector("#mcp-editor", state="hidden")
        settle(page)
        edited = config()["servers"]["Live One"]
        check("a blank secret on edit keeps the stored value", edited["env"] == {"API_KEY": "topsecret-123"}, edited)
        check("the description changed", edited["description"] == "renamed description")
        check("a description-only edit did NOT disconnect it (still ready, no new refresh)",
              "ready" in card(page, "Live One").inner_text().lower(), card(page, "Live One").inner_text())

        # ---- rename -------------------------------------------------------------
        card(page, "Live One").get_by_text("Edit").click()
        page.wait_for_selector("#mcp-editor:not([hidden])")
        page.fill("#mcp-f-name", "Renamed")
        page.click("#mcp-f-save")
        page.wait_for_selector('.mcp-card[data-server="Renamed"]', timeout=60000)
        settle(page)
        check("a rename replaces the card and the config key", cards(page) == ["Renamed"] and list(config()["servers"]) == ["Renamed"],
              (cards(page), list(config()["servers"])))
        check("the rename reconnected under the new tool prefix",
              "ready" in card(page, "Renamed").inner_text().lower() and "mcp_renamed_" in card(page, "Renamed").inner_text(),
              card(page, "Renamed").inner_text())

        # ---- a refused save stays in the form ------------------------------------
        page.click("#btn-mcp-add")
        page.wait_for_selector("#mcp-editor:not([hidden])")
        page.fill("#mcp-f-name", "renamed")          # same tool prefix as "Renamed"
        page.fill("#mcp-f-command", "npx")
        page.click("#mcp-f-save")
        page.wait_for_function("!document.querySelector('.mcp-editor__error').hidden")
        check("a server-side refusal (duplicate prefix) is shown in the form and the form stays open",
              "prefix" in page.inner_text(".mcp-editor__error") and page.locator("#mcp-editor").is_visible(),
              page.inner_text(".mcp-editor__error"))
        check("and the form kept what was typed", page.input_value("#mcp-f-command") == "npx")
        page.click("text=Cancel")
        check("Cancel returns to the list", page.locator("#mcp-list-view").is_visible() and page.locator("#mcp-editor").is_hidden())

        # ---- a remote server whose URL carries a token ---------------------------
        page.click("#btn-mcp-add")
        page.fill("#mcp-f-name", "Remote")
        page.select_option("#mcp-f-transport", "http")
        check("choosing Remote shows the URL field and hides the command", page.locator("#mcp-f-url").is_visible() and page.locator("#mcp-f-command").is_hidden())
        page.fill("#mcp-f-url", "ftp://nope")
        page.click("#mcp-f-save")
        check("a non-http URL is refused before any request", "http" in page.inner_text(".mcp-editor__error")
              and "Remote" not in config()["servers"])
        page.fill("#mcp-f-url", "https://user:pw@h.example:8443/mcp?token=SECRETTOKEN")
        page.uncheck("#mcp-f-enabled")
        page.click("#mcp-f-save")
        page.wait_for_selector('.mcp-card[data-server="Remote"]')
        settle(page)
        card(page, "Remote").get_by_text("Details").click()
        check("the URL's login and token are not on the page", all(x not in page.content() for x in ("SECRETTOKEN", "user:pw", "pw@")))
        check("the card says part of it is hidden", "hidden" in card(page, "Remote").inner_text().lower())
        card(page, "Remote").get_by_text("Edit").click()
        page.wait_for_selector("#mcp-editor:not([hidden])")
        check("editing shows an empty URL box that says it is unchanged", page.input_value("#mcp-f-url") == ""
              and "unchanged" in page.get_attribute("#mcp-f-url", "placeholder"))
        page.fill("#mcp-f-desc", "remote desc")
        page.click("#mcp-f-save")
        page.wait_for_selector("#mcp-editor", state="hidden")
        settle(page)
        check("saving with the URL box empty kept the stored URL",
              config()["servers"]["Remote"]["url"] == "https://user:pw@h.example:8443/mcp?token=SECRETTOKEN")

        # ---- a broken server is explained in words --------------------------------
        page.click("#btn-mcp-add")
        page.fill("#mcp-f-name", "Broken")
        page.fill("#mcp-f-command", "definitely-not-a-real-program-xyz")
        page.click("#mcp-f-save")
        page.wait_for_selector('.mcp-card[data-server="Broken"]', timeout=60000)
        settle(page)
        c = card(page, "Broken")
        check("a server that can't start shows Error and the reason, in a visible box",
              c.locator(".mcp-card__state").inner_text().strip().lower() == "error"
              and c.locator(".mcp-card__problem").is_visible()
              and "definitely-not-a-real-program-xyz" in c.locator(".mcp-card__problem").inner_text(),
              c.inner_text())
        check("Needs attention counts it", "1" == page.inner_text(".mcp-chip:has-text('Needs attention') .mcp-chip__n"),
              page.inner_text(".mcp-chips"))
        shot(page, "04-error-card")

        # ---- filters and search ----------------------------------------------------
        page.click(".mcp-chip:has-text('Needs attention')")
        check("the Needs attention chip shows only the broken one", cards(page) == ["Broken"], cards(page))
        page.click(".mcp-chip:has-text('Off')")
        check("the Off chip shows the switched-off remote one", cards(page) == ["Remote"], cards(page))
        page.click(".mcp-chip:has-text('All')")
        page.fill("#mcp-search", "ping")
        check("searching a TOOL name finds the server that has it", cards(page) == ["Renamed"], cards(page))
        page.fill("#mcp-search", "zzzzqqq")
        check("no match shows an explanation and a way back", page.locator(".mcp-empty__title").inner_text() == "No server matches"
              and page.locator(".mcp-empty .btn").count() == 1)
        page.click(".mcp-empty .btn")
        check("Clear filters restores everything", len(cards(page)) == 3 and page.input_value("#mcp-search") == "")

        # ---- keyboard ------------------------------------------------------------------
        page.focus("body")
        page.keyboard.press("/")
        check("/ focuses the search box", page.evaluate("document.activeElement.id") == "mcp-search")
        page.keyboard.press("Escape")   # still typing in the box: Esc closes the panel
        check("Esc closes the panel", page.locator("#mcp-overlay").is_hidden())
        page.evaluate("window.JarvisHost.openPanel('mcp')")
        page.click("#btn-mcp-add")
        page.keyboard.press("Escape")
        check("Esc inside the editor closes only the editor", page.locator("#mcp-overlay").is_visible() and page.locator("#mcp-editor").is_hidden())

        # ---- remove -----------------------------------------------------------------------
        card(page, "Broken").get_by_text("Remove").click()
        page.wait_for_selector(".jui-modal")
        page.wait_for_function("document.activeElement && document.activeElement.textContent === 'Keep it'", timeout=3000)
        check("Remove asks first and defaults to Keep it", True)
        page.click(".jui-modal >> text=Keep it")
        page.wait_for_selector(".jui-modal", state="detached")
        check("Keep it removes nothing", "Broken" in config()["servers"])
        card(page, "Broken").get_by_text("Remove").click()
        page.wait_for_selector(".jui-modal")
        page.click(".jui-modal .jui-btn:has-text(\"Remove\")")
        page.wait_for_selector('.mcp-card[data-server="Broken"]', state="detached")
        check("confirming removes the card and the config entry", "Broken" not in config()["servers"] and "Broken" not in cards(page))

        # ---- Refresh all ------------------------------------------------------------------------
        before = len([c for c in CALLS if c[1] == "/api/mcp/refresh"])
        page.click("#btn-mcp-refresh")
        page.wait_for_function("document.getElementById('btn-mcp-refresh').textContent === 'Refresh all'", timeout=60000)
        check("Refresh all sends one request for every server", len([c for c in CALLS if c[1] == "/api/mcp/refresh"]) == before + 1
              and [c for c in CALLS if c[1] == "/api/mcp/refresh"][-1][2] == {})
        shot(page, "05-final")

        # ---- small screen ------------------------------------------------------------------------
        page.set_viewport_size({"width": 390, "height": 760})
        overflow = page.evaluate("""() => { const p = document.querySelector('#mcp-overlay .menu-panel'); return p.scrollWidth - p.clientWidth; }""")
        check("at phone width nothing overflows sideways", overflow <= 1, overflow)
        shot(page, "06-phone")

        check("no uncaught page errors", not errors, errors)
        browser.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print("  -", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
