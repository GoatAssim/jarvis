"""L.36-P17 - allowed servers editor, status dropdown and the Home status cards, in a real browser.

Loads web/public/index.html with the shipped channels-panel.js / channels.css in
headless Chromium. Every /api/ answer comes from the REAL backend: the routes below
call the same `channels_cli.handle(...)` entry point web/server.js shells out to (a
throwaway HOME, never the real ~/.jarvis). web/server.js itself is NOT run (it
needs express); tests/test_allowed_guilds.py pins its route statically.

Covers
  BUG-A  Home (the old side column, now picked like a person) is pinned above the list
         but is not one of the people; its Platforms tab shows the Discord AND Instagram
         status cards at full height, and with a long server list the Servers tab's
         list scrolls inside its own box
  BUG-B  the status filter is ONE dropdown (not a row of chips) with a count per option,
         and choosing an option filters the people list
  BUG-C  allowed servers: pick -> Allow previews first (names who goes quiet) and
         writes nothing until "Yes, allow it"; Cancel sends nothing; an id that is not a
         seen server previews too; a second seen server applies at once; junk is
         explained without a request; the LAST entry's remove button is disabled and the
         terminal route is explained; what was typed survives a re-render

Needs `playwright` (Python) + a launchable Chromium; prints SKIP and exits 0 without them.

    python3 tests/verify_l36_guilds_ui.py
"""
import contextlib, io, json, mimetypes, os, sys, tempfile
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed"); sys.exit(0)

HERE = Path(__file__).resolve().parent
PUB = HERE.parent / "web" / "public"
_HOME = tempfile.mkdtemp(prefix="jarvis-guilds-ui-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
for v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT"):
    os.environ.pop(v, None)
sys.path.insert(0, str(HERE.parent / "jarvis-cli"))

from jarvis import channels_cli                                   # noqa: E402
from jarvis.channels import DISCORD, people, servers             # noqa: E402
from jarvis.channels import config as channel_config             # noqa: E402

OWNER, FRIEND, BLOCKED = "100000000000000001", "200000000000000002", "300000000000000003"
N_SERVERS = 40
GID = lambda i: str(900000000000000000 + i)  # noqa: E731
UNSEEN = "900000000000009999"

for uid, handle in ((OWNER, "boss"), (FRIEND, "friend"), (BLOCKED, "badguy")):
    people.touch(DISCORD, uid, handle=handle)
cfg0 = channel_config.load_config()
cfg0[DISCORD].update({"enabled": True, "owner": OWNER, "allow_tools": False, "bot_token": "dummy-token",
                      "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND]})
channel_config.save_config(cfg0)
for i in range(1, N_SERVERS + 1):
    servers.note_guild(DISCORD, GID(i), f"Server {i:02d}",
                       channels=[(str(800000000000000000 + i * 10 + c), f"chan-{c}", "text") for c in range(8)])


def cli(*argv):
    buf = io.StringIO(); code = 0
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            channels_cli.handle(list(argv))
        except SystemExit as exc:
            code = exc.code or 0
    try:
        return code, json.loads(buf.getvalue())
    except ValueError:
        return code, {"raw": buf.getvalue()}


# block one person the same way the panel's flag route does
cli("channels-user", DISCORD, BLOCKED, "blocked", "on")

posts = []   # (path, body) for every non-GET /api/ call


def handle(route):
    req = route.request
    url = req.url
    path = "/" + (url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url)
    path = path.split("?")[0]
    j = lambda body, status=200: route.fulfill(status=status, content_type="application/json", body=json.dumps(body))  # noqa: E731
    body = json.loads(req.post_data or "{}") if req.method != "GET" else {}
    if req.method != "GET" and path.startswith("/api/"):
        posts.append((path, body))
    if path == "/api/channels/people":
        return j(cli("channels-users")[1])
    if path == "/api/channels":
        return j({"ok": True, "config": {}})
    if path == "/api/channels/denied":
        return j(cli("channels-denied", DISCORD, "14")[1])
    if path == "/api/channels/servers":
        code, out = cli("channels-servers", DISCORD)
        return j(out, 200 if code == 0 else 400)
    if path == "/api/channels/servers/discord/allowed-guilds" and req.method == "POST":
        # the same argv server.js builds (see tests/test_allowed_guilds.py for the static pin)
        args = ["channels-guilds", DISCORD, "remove" if body.get("remove") is True else "add", str(body.get("id", ""))]
        if body.get("confirm") is True and body.get("remove") is not True:
            args.append("--yes")
        code, out = cli(*args)
        return j(out, 200 if out.get("ok") is not False else 400)
    if path.startswith("/api/channels/servers/discord/") and req.method == "POST":
        _, _, _, _, kind, ident = path.split("/")[:6]
        code, out = cli("channels-server-set", DISCORD, kind, ident, body["switch"], body["value"])
        return j(out, 200 if code == 0 else 400)
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


def check(name, cond, detail=""):
    res.append(bool(cond)); print(("ok      " if cond else "FAILED  ") + name + (f"   [{detail}]" if detail and not cond else ""))


def settle(pred, ms=3000):
    for _ in range(ms // 50):
        if pred():
            return True
        pg.wait_for_timeout(50)
    return pred()


def gposts():
    return [b for p, b in posts if p.endswith("/allowed-guilds")]


def allowed_now():
    return cli("channels-guilds", DISCORD)[1]["allowed_guilds"]


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

    # ---- BUG-A: Home, its status cards and the long server list
    check("A: the old side column is gone", pg.locator("#ch-side").count() == 0)
    check("A: Home is pinned above the list, and is not one of the people",
          pg.locator("#ch-home .ch-card--home").count() == 1 and pg.locator("#ch-list .ch-card--home").count() == 0)
    pg.locator("#ch-home .ch-card--home").click()
    pg.wait_for_selector("#ch-pane-platforms .ch-plat")
    check("A: Home shows as selected", "is-active" in (pg.locator("#ch-home .ch-card--home").get_attribute("class") or ""))
    d_box = pg.locator("#ch-pane-platforms .ch-plat[data-plat='discord']").bounding_box()
    i_box = pg.locator("#ch-pane-platforms .ch-plat[data-plat='instagram']").bounding_box()
    check("A: the Discord status card keeps a real height", d_box and d_box["height"] >= 100)
    check("A: the Instagram status card keeps a real height", i_box and i_box["height"] >= 100)
    check("A: Instagram's status row is readable (not squashed to nothing)",
          pg.locator("#ch-pane-platforms .ch-plat[data-plat='instagram'] .ch-kv").first.bounding_box()["height"] >= 15)
    check("A: both cards are on screen without scrolling", max(d_box["y"] + d_box["height"], i_box["y"] + i_box["height"]) < 900)
    pg.locator("#ch-tab-servers").click()
    pg.wait_for_selector(".ch-server")
    lst = pg.locator(".ch-servers__list")
    dims = lst.evaluate("e => ({ch: e.clientHeight, sh: e.scrollHeight})")
    check("A: the long list scrolls inside its own box", dims["sh"] > dims["ch"] and dims["ch"] <= 421, str(dims))
    check("A: the list really holds every server", lst.locator(".ch-server").count() == N_SERVERS)

    # ---- BUG-B: the status dropdown
    sel = pg.locator("#ch-state-select")
    check("B: the status filter is a dropdown", sel.count() == 1 and sel.evaluate("e => e.tagName") == "SELECT")
    check("B: ...and the old chip row is gone", pg.locator("#ch-state-chips .ch-chip").count() == 0)
    opts = sel.locator("option").all_inner_texts()
    check("B: six options, each with a count", len(opts) == 6 and all("(" in o and o.endswith(")") for o in opts), str(opts))
    check("B: the platform chips are untouched", pg.locator("#ch-platform-chips .ch-chip").count() == 3)
    total = pg.locator("#ch-list .ch-card").count()
    sel.select_option("blocked")
    check("B: choosing Blocked filters the list to the blocked person",
          settle(lambda: pg.locator("#ch-list .ch-card").count() == 1) and total > 1)
    check("B: the dropdown keeps the choice and keeps keyboard focus",
          pg.locator("#ch-state-select").input_value() == "blocked"
          and pg.evaluate("document.activeElement && document.activeElement.id") == "ch-state-select")
    pg.locator("#ch-state-select").select_option("all")
    check("B: All brings everyone back", settle(lambda: pg.locator("#ch-list .ch-card").count() == total))

    # ---- BUG-C: allowed servers
    box = pg.locator("[data-allowed-guilds]")
    check("C: with no filter it says so", "no filter" in box.inner_text().lower())
    check("C: the picker lists the seen servers", box.locator(".ch-allowed__select option").count() == N_SERVERS + 1)
    box.locator(".ch-allowed__select").select_option(GID(1))
    box.locator(".ch-allowed__btn").click()
    check("C: Allow on an empty list sends a request WITHOUT confirm",
          settle(lambda: len(gposts()) == 1) and gposts()[0] == {"id": GID(1), "remove": False, "confirm": False}, str(gposts()))
    check("C: ...and the page asks first, naming who would go quiet",
          settle(lambda: pg.locator(".ch-allowed__confirm").count() == 1)
          and "would stop answering" in pg.locator(".ch-allowed__confirm").inner_text()
          and "Server 02" in pg.locator(".ch-allowed__confirm").inner_text())
    check("C: ...and nothing was written", allowed_now() == [])
    pg.locator(".ch-allowed__confirm .btn", has_text="Cancel").click()
    check("C: Cancel closes it and sends nothing", settle(lambda: pg.locator(".ch-allowed__confirm").count() == 0) and len(gposts()) == 1 and allowed_now() == [])
    box.locator(".ch-allowed__select").select_option(GID(1))
    box.locator(".ch-allowed__btn").click()
    pg.wait_for_selector(".ch-allowed__confirm")
    pg.locator(".ch-allowed__confirm .btn--primary").click()
    check("C: 'Yes, allow it' is the only click that sends confirm:true",
          settle(lambda: len(gposts()) == 3) and gposts()[-1] == {"id": GID(1), "remove": False, "confirm": True}, str(gposts()))
    check("C: ...and the filter is on, with the server shown by name",
          settle(lambda: pg.locator(".ch-allowed__chips .channels-chip").count() == 1)
          and "Server 01" in pg.locator(".ch-allowed__chips").inner_text() and allowed_now() == [GID(1)])
    check("C: the note switches to 'only these servers'", "only these servers" in box.inner_text().lower())
    check("C: the allowed server is no longer offered in the picker", box.locator(f".ch-allowed__select option[value='{GID(1)}']").count() == 0)
    check("C: with ONE entry, its remove button is disabled and the terminal route is explained",
          box.locator(".channels-chip__remove").first.is_disabled() and "channels-set discord allowed_guilds []" in box.inner_text())
    check("C: servers off the list now read as not answering in their card",
          "not answering" in pg.locator(f".ch-server[data-server='{GID(2)}']").inner_text().lower())

    n = len(gposts())
    box.locator(".ch-allowed__select").select_option(GID(2))
    box.locator(".ch-allowed__btn").click()
    check("C: a second SEEN server applies at once (no confirm box)",
          settle(lambda: allowed_now() == [GID(1), GID(2)]) and pg.locator(".ch-allowed__confirm").count() == 0
          and gposts()[n] == {"id": GID(2), "remove": False, "confirm": False}, str(gposts()[n:]))
    check("C: ...and now two chips, and remove is enabled",
          settle(lambda: pg.locator(".ch-allowed__chips .channels-chip").count() == 2
                 and not pg.locator("[data-allowed-guilds] .channels-chip__remove").first.is_disabled()))

    n = len(gposts())
    box.locator(".ch-allowed__id").fill(UNSEEN)
    box.locator(".ch-allowed__btn").click()
    check("C: an id the bot has never seen asks first",
          settle(lambda: pg.locator(".ch-allowed__confirm").count() == 1) and "hasn't seen" in pg.locator(".ch-allowed__confirm").inner_text()
          and allowed_now() == [GID(1), GID(2)] and gposts()[n]["confirm"] is False)
    pg.locator(".ch-allowed__confirm .btn", has_text="Cancel").click()

    n = len(gposts())
    box.locator(".ch-allowed__id").fill("abc")
    box.locator(".ch-allowed__btn").click()
    check("C: junk is explained and sends NO request",
          settle(lambda: "digits only" in pg.locator("[data-allowed-guilds]").inner_text()) and len(gposts()) == n)

    # typed text survives a re-render (a server switch reloads the side column)
    pg.locator("[data-allowed-guilds] .ch-allowed__id").fill("12345")
    pg.locator(f".ch-server[data-server='{GID(3)}'] summary").click()
    pg.locator(f".ch-server[data-server='{GID(3)}'] .ch-switch").first.click()
    settle(lambda: any(pth.endswith(f"/guild/{GID(3)}") for pth, _ in posts))
    pg.wait_for_timeout(300)
    check("C: what was typed in the id box survives a reload of the side column",
          pg.locator("[data-allowed-guilds] .ch-allowed__id").input_value() == "12345")
    pg.locator("[data-allowed-guilds] .ch-allowed__id").fill("")

    n = len(gposts())
    pg.locator(f".ch-allowed__chips .channels-chip[data-guild='{GID(2)}'] .channels-chip__remove").click()
    check("C: removing one of two applies at once and sends remove:true",
          settle(lambda: allowed_now() == [GID(1)]) and gposts()[n] == {"id": GID(2), "remove": True, "confirm": False}, str(gposts()[n:]))
    check("C: back to one entry: remove is disabled again", settle(lambda: box.locator(".channels-chip__remove").first.is_disabled()))
    check("C: the last entry was never removable from the page", GID(1) in allowed_now())

    check("no page errors", not errs)
    if errs:
        print("  page errors:", errs[:3])
    b.close()

print(f"\n{sum(res)} passed, {len(res) - sum(res)} failed")
sys.exit(0 if all(res) else 1)
