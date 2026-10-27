"""Download Hub, N23 part f: the Updates tab -- the hub_admin ops behind it, the three web routes, the panel's files,
the two settings rows and the owner-only rule for `dl_updates_check`.

Covers: the three ops are hooked into hub_admin (`updates` is a read, `updates_check` and `updates_dismiss` are writes
and are refused for every kind of unattended process while the read still works); the answer a panel needs (findings,
a health line per provider, the daemon flag, the limits); "Ignore this version" hides one finding and a different
`available` brings it back; a provider with its update checks switched off leaves the Updates answer but stays in the
Settings payload (`update_settings`), so the switch can be turned back on; input that is not a provider id or a finding id
is refused; Check now says "daemon_down" when no daemon runs; `jarvis hub-admin updates` works through run_cli.
Static checks on `web/server.js` (the routes, their guards, their input caps), on the panel's HTML / JS / CSS (the tab,
the pane, textContent only, no Update button, no source filter, no notification of its own, writes carry the hub header)
and on `tools.py` (`dl_updates_check` and `dl_updates` are both owner-only since 9B).

No network, no daemon, no browser.  The tab's pure helpers (updateGroups, updateHealthText, updateCheckNotice, ...) have NO
JavaScript test yet: an earlier draft of this file named tests/verify_hub_updates_tab.js, which was never delivered.
Run: python tests/test_hub_updates_tab.py
"""
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-upd-tab-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
UNATTENDED_VARS = ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT")
for _var in UNATTENDED_VARS + ("JARVIS_ALLOWED_TOOLS", "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import hub_admin, tools  # noqa: E402
from jarvis.hub import service, updates_admin  # noqa: E402
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


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


CLOCK = Clock()
STORE = Store(Path(HOME) / "hub-tab.db", clock=CLOCK)
# `fetch`: a provider with an update tool.  `pushy`: one that only reports findings itself (no update tool).
STORE.register_provider("fetch", label="Fetch", mode="push")
STORE.update_check_put("fetch", update_tool="check_fetch_updates", checked_at=CLOCK() - 300, ok_at=CLOCK() - 300)
STORE.register_provider("pushy", label="Pushy", mode="push")
STORE.daemon_beat(os.getpid(), CLOCK())

ITEMS = [
    {"id": "llama", "title": "llama-8b-q4.gguf", "installed": "v1.4", "available": "v1.5", "size_bytes": 4900000000, "source": "models"},
    {"id": "tiny", "title": "tiny.gguf", "installed": "v0.1", "available": "v0.2"},
]
applied = STORE.update_apply("fetch", ITEMS, complete=True, now=CLOCK() - 300)
STORE.update_offer("pushy", "game-1", {"title": "A game", "installed": "1", "available": "2"}, now=CLOCK() - 60)


def admin(op, request=None):
    return hub_admin.admin(op, request or {}, store=STORE)


# --------------------------------------------------------------------------- the ops are hooked up

def test_ops():
    check("`updates` is a hub_admin read op, the other two are write ops",
          "updates" in hub_admin.READ_OPS and "updates_check" in hub_admin.WRITE_OPS and "updates_dismiss" in hub_admin.WRITE_OPS
          and "updates" not in hub_admin.WRITE_OPS)
    check("all three are in OPS (so the usage line and run_cli know them)", {"updates", "updates_check", "updates_dismiss"} <= set(hub_admin.OPS))
    check("the setup built a finding for the fetch provider", applied.get("applied") == 2)

    out = admin("updates")
    check("`updates` answers ok with the findings of both providers", out.get("ok") is True and out.get("count") == 3 and len(out["updates"]) == 3)
    by = {(f["provider"], f["id"]): f for f in out["updates"]}
    check("a finding carries title, installed, available, size, source and its age",
          by[("fetch", "llama")]["title"] == "llama-8b-q4.gguf" and by[("fetch", "llama")]["installed"] == "v1.4"
          and by[("fetch", "llama")]["available"] == "v1.5" and by[("fetch", "llama")]["size_bytes"] == 4900000000
          and by[("fetch", "llama")]["source"] == "models" and by[("fetch", "llama")]["age_s"] == 300)
    check("the hub never compares versions: it hands back the two texts as the source gave them",
          by[("fetch", "tiny")]["installed"] == "v0.1" and by[("fetch", "tiny")]["available"] == "v0.2" and "newer" not in json.dumps(by[("fetch", "tiny")]))
    provs = {p["provider"]: p for p in out["providers"]}
    check("each provider has a health line: status, can_check, age, count",
          provs["fetch"]["status"] == "ok" and provs["fetch"]["can_check"] is True and provs["fetch"]["count"] == 2
          and provs["pushy"]["status"] == "pushed" and provs["pushy"]["can_check"] is False)
    check("the answer says whether the daemon is running and what the limits are",
          out.get("daemon_alive") is True and out["limits"]["interval_h_min"] == 0.5 and out["limits"]["manual_check_s"] > 0)
    only = admin("updates", {"provider": "fetch"})
    check("`updates` can be asked for one provider", only["count"] == 2 and all(f["provider"] == "fetch" for f in only["updates"]))
    check("a provider id that is not an id is refused, not searched",
          admin("updates", {"provider": "not an id!"}).get("ok") is False and admin("updates", {"provider": "../x"}).get("ok") is False)


# --------------------------------------------------------------------------- the writes' guards

def test_unattended():
    for var in UNATTENDED_VARS:
        os.environ[var] = "scheduled" if var == "JARVIS_CONTEXT" else "1"
        try:
            c = admin("updates_check", {})
            d = admin("updates_dismiss", {"provider": "fetch", "id": "tiny"})
            r = admin("updates")
        finally:
            del os.environ[var]
        check("%s: Check now is refused (code unattended)" % var, c.get("ok") is False and c.get("code") == "unattended")
        check("%s: Ignore this version is refused and hides nothing" % var,
              d.get("ok") is False and d.get("code") == "unattended"
              and any(f["id"] == "tiny" for f in admin("updates")["updates"]))
        check("%s: the read still works (a chat reply may show the card)" % var, r.get("ok") is True)
    # updates_admin refuses by itself too, for a caller that skips hub_admin
    os.environ["JARVIS_CHANNEL"] = "1"
    try:
        direct = updates_admin.updates_check({}, store=STORE), updates_admin.updates_dismiss({"provider": "fetch", "id": "tiny"}, store=STORE)
    finally:
        del os.environ["JARVIS_CHANNEL"]
    check("hub/updates_admin refuses an unattended process by itself as well", all(x.get("ok") is False and x.get("code") == "unattended" for x in direct))


def test_inputs():
    for bad in ({"provider": "fetch"}, {"provider": "fetch", "id": ""}, {"id": "tiny"}, {"provider": "fetch", "id": "a/b"},
                {"provider": "fetch", "id": "x" * 200}, {"provider": "Fetch Me", "id": "tiny"}, {"provider": "fetch", "id": "tiny; rm -rf"}):
        out = admin("updates_dismiss", bad)
        check("Ignore this version refuses %r" % (str(bad)[:50],), out.get("ok") is False)
    check("nothing was hidden by the refused requests", len(admin("updates")["updates"]) == 3)
    check("Ignore this version for a finding that does not exist is a plain refusal",
          admin("updates_dismiss", {"provider": "fetch", "id": "nope"}).get("ok") is False)
    bad = admin("updates_check", {"provider": "not an id!"})
    check("Check now refuses a provider id that is not an id", bad.get("ok") is False)


def test_check_without_daemon():
    other = Store(Path(HOME) / "hub-nodaemon.db", clock=CLOCK)
    other.register_provider("fetch", label="Fetch", mode="push")
    other.update_check_put("fetch", update_tool="check_fetch_updates")
    out = hub_admin.admin("updates_check", {}, store=other)
    check("Check now with no daemon running says so (code daemon_down)", out.get("ok") is False and out.get("code") == "daemon_down" and "daemon" in out.get("error", ""))
    st = other.update_check_state().get("fetch") or {}
    check("...and sets no request flag", not st.get("requested_at"))
    alive = admin("updates_check", {"provider": "fetch"})
    check("with the daemon running, Check now sets the flag for the runner and says the check starts within a few seconds",
          alive.get("ok") is True and alive["results"][0]["status"] == "requested" and "few seconds" in alive["results"][0]["message"])
    again = admin("updates_check", {"provider": "fetch"})
    check("a second press at once is answered, not queued twice (already checking, or too soon)",
          again.get("ok") is True and again["results"][0]["status"] in ("already_checking", "too_soon"))
    nope = admin("updates_check", {"provider": "pushy"})
    check("a provider with no update check cannot be checked and says why", nope.get("ok") is False and "no update check" in nope.get("error", ""))


def test_dismiss_and_return():
    out = admin("updates_dismiss", {"provider": "fetch", "id": "tiny"})
    check("Ignore this version answers ok and says it stays hidden until the source reports a different version",
          out.get("ok") is True and "different version" in out.get("note", "") and out["hidden"]["available"] == "v0.2")
    after = admin("updates")
    check("the ignored finding is gone from the list and from the count",
          after["count"] == 2 and not any(f["id"] == "tiny" for f in after["updates"]))
    shown = admin("updates", {"include_hidden": True})
    tiny = [f for f in shown["updates"] if f["id"] == "tiny"]
    check("include_hidden lists it again, marked hidden (nothing was deleted)", len(tiny) == 1 and tiny[0]["hidden"] is True)
    STORE.update_apply("fetch", [dict(ITEMS[0]), dict(ITEMS[1])], complete=True, now=CLOCK())
    check("the same version reported again stays hidden", not any(f["id"] == "tiny" for f in admin("updates")["updates"]))
    STORE.update_apply("fetch", [dict(ITEMS[0]), dict(ITEMS[1], available="v0.3")], complete=True, now=CLOCK())
    back = [f for f in admin("updates")["updates"] if f["id"] == "tiny"]
    check("a DIFFERENT available version brings it back", len(back) == 1 and back[0]["available"] == "v0.3")
    STORE.update_apply("fetch", [dict(ITEMS[0])], complete=True, now=CLOCK())
    check("a finding the source stops listing disappears by itself (complete: true)", not any(f["id"] == "tiny" for f in admin("updates")["updates"]))
    STORE.update_apply("fetch", [dict(ITEMS[0]), dict(ITEMS[1])], complete=True, now=CLOCK())   # put it back for the rest


def test_provider_text_is_data():
    evil = "<img src=x onerror=alert(1)>\n\x1b[31mred\x07" + "A" * 500
    STORE.update_apply("fetch", [{"id": "evil", "title": evil, "installed": evil, "available": evil, "source": evil}], complete=False, now=CLOCK())
    f = [x for x in admin("updates")["updates"] if x["id"] == "evil"]
    check("a hostile name is kept as text, on one printable line and capped (the tab draws it with textContent)",
          len(f) == 1 and "\n" not in f[0]["title"] and "\x1b" not in f[0]["title"] and len(f[0]["title"]) <= 200
          and len(f[0]["installed"]) <= 80 and len(f[0]["source"]) <= 40)
    STORE.update_apply("fetch", [dict(ITEMS[0]), dict(ITEMS[1])], complete=True, now=CLOCK())


# --------------------------------------------------------------------------- the Settings rows' data

def test_settings_payload():
    pay = hub_admin.settings_payload(STORE)
    check("the settings answer still opens", pay.get("ok") is True and "update_settings" in pay)
    info = pay["update_settings"]
    ids = [p["provider"] for p in info["providers"]]
    check("a provider with an update check is listed, one that only reports its own findings is not", "fetch" in ids and "pushy" not in ids)
    mine = [p for p in info["providers"] if p["provider"] == "fetch"][0]
    check("its two settings come with their defaults (on, every 24 h)", mine["update_checks"] is True and mine["update_interval_h"] == 24.0 and mine["has_check"] is True)
    check("the limits are the hub's own", info["limits"] == {"interval_h_min": 0.5, "interval_h_max": 720.0, "interval_h_default": 24.0})

    r = service.update_provider("fetch", settings={"update_checks": False}, store=STORE)
    check("dl_provider_prefs' own function accepts update_checks", r.get("ok") is True)
    check("with update_checks off the provider leaves the Updates answer (findings and health)",
          not any(f["provider"] == "fetch" for f in admin("updates")["updates"]) and not any(p["provider"] == "fetch" for p in admin("updates")["providers"]))
    off = [p for p in hub_admin.settings_payload(STORE)["update_settings"]["providers"] if p["provider"] == "fetch"]
    check("...but it stays in the Settings payload, switched off, so the switch can be turned back on", len(off) == 1 and off[0]["update_checks"] is False)
    check("Check now for it says the checks are off", "switched off" in admin("updates_check", {"provider": "fetch"}).get("error", ""))
    service.update_provider("fetch", settings={"update_checks": True, "update_interval_h": 6}, store=STORE)
    back = [p for p in hub_admin.settings_payload(STORE)["update_settings"]["providers"] if p["provider"] == "fetch"][0]
    check("turned back on with a 6 hour interval, both show", back["update_checks"] is True and back["update_interval_h"] == 6.0
          and any(p["provider"] == "fetch" for p in admin("updates")["providers"]))
    bad = service.update_provider("fetch", settings={"update_interval_h": 0.01}, store=STORE)
    check("an interval below the limit is refused by the hub's own validator", bad.get("ok") is False)
    service.update_provider("fetch", settings={"update_interval_h": 24}, store=STORE)

    service.update_provider("pushy", settings={"update_checks": False}, store=STORE)
    push = [p for p in hub_admin.settings_payload(STORE)["update_settings"]["providers"] if p["provider"] == "pushy"]
    check("a provider that only reports its own findings is listed once its checks are off (the way back)", len(push) == 1 and push[0]["has_check"] is False)
    service.update_provider("pushy", settings={"update_checks": True}, store=STORE)
    check("a store that cannot be read gives an empty list, not a crash", updates_admin.provider_update_settings(object())["providers"] == [])


def test_cli():
    out = io.StringIO()
    code = hub_admin.run_cli(["updates"], stdin=io.StringIO("{}"), stdout=out)
    line = out.getvalue().strip().splitlines()[-1] if out.getvalue().strip() else ""
    try:
        parsed = json.loads(line)
    except ValueError:
        parsed = {}
    check("`jarvis hub-admin updates` reads one JSON object and prints one (ok, whatever the default store holds)", code == 0 and "ok" in parsed)


# --------------------------------------------------------------------------- the web routes (static)

SERVER = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
UI = ROOT / "jarvis-cli" / "jarvis" / "actions" / "hub_ui"
HTML = (UI / "tool.html").read_text(encoding="utf-8")
JS = (UI / "tool.js").read_text(encoding="utf-8")
CSS = (UI / "tool.css").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "web" / "public" / "downloads-settings.js").read_text(encoding="utf-8")


def test_routes():
    routes = re.findall(r'app\.(get|post|put|delete|patch)\("(/api/hub/updates[^"]*)", ([A-Za-z]+), ([A-Za-z]+),', SERVER)
    check("there are exactly three /api/hub/updates routes", sorted((m, p) for m, p, _, _ in routes) ==
          [("get", "/api/hub/updates"), ("post", "/api/hub/updates/check"), ("post", "/api/hub/updates/dismiss")])
    guards = {p: (a, b) for _, p, a, b in routes}
    check("the read route is behind the loopback / same-origin check and the Jarvis check", guards.get("/api/hub/updates") == ("requireHubRead", "requireJarvis"))
    check("both writes also need the X-Jarvis-Hub header (requireHubWrite)",
          guards.get("/api/hub/updates/check") == ("requireHubWrite", "requireJarvis") and guards.get("/api/hub/updates/dismiss") == ("requireHubWrite", "requireJarvis"))
    check("the ids are length-capped before they reach hub_admin (provider 40, finding id 120) and nothing else is passed on",
          'hubAdmin(res, "updates", {\n  provider: hubText(req.query.provider, 40), include_hidden: req.query.include_hidden === "1",\n})' in SERVER
          and 'hubAdmin(res, "updates_check", {\n  provider: hubText(req.body?.provider, 40),\n})' in SERVER
          and 'hubAdmin(res, "updates_dismiss", {\n  provider: hubText(req.body?.provider, 40), id: hubText(req.body?.id, 120),\n})' in SERVER)
    check("no route installs, upgrades or runs anything else: the three ops are the only ones the routes name", len(re.findall(r'hubAdmin\(res, "updates[a-z_]*"', SERVER)) == 3)


# --------------------------------------------------------------------------- the panel's files (static)

def region(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


UPD_JS = region(JS, "// ---- Updates (N23 part f): what the hub found out of date.", "// ---- History (brief 25, N21)\n\nlet histTimer")
UPD_PURE = region(JS, "// ---- Updates (N23 part f): the words and the order", "const pure = {")


def test_panel_files():
    check("the tab button, the pane and the badge exist and point at each other",
          'id="tabUpdates"' in HTML and 'aria-controls="updatesPane"' in HTML and 'data-tab="updates"' in HTML and 'id="updatesPane"' in HTML
          and 'aria-labelledby="tabUpdates"' in HTML and 'id="updCount"' in HTML and 'id="updList"' in HTML and 'id="updSummary"' in HTML and 'id="updCheck"' in HTML)
    check("the tab sits between Sources and History", HTML.index('id="tabSources"') < HTML.index('id="tabUpdates"') < HTML.index('id="tabHistory"'))
    check("tool.js knows the tab in setTab and in the arrow keys",
          JS.count('"downloads", "sources", "updates", "history"') == 3 and "$(\"#updatesPane\").hidden" in JS)
    ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', UPD_JS))
    have = set(re.findall(r'id="([A-Za-z0-9_-]+)"', HTML))
    check("every id the Updates code looks up exists in tool.html", ids and ids <= have)
    check("the Updates code never builds markup from data: no innerHTML, outerHTML, insertAdjacentHTML, document.write or eval",
          not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", UPD_JS + UPD_PURE))
    check("provider text only ever reaches the page through el(...text:) (textContent) or an attribute", "textContent" in UPD_JS and ".html" not in UPD_JS.replace("tool.html", ""))
    check("the reads and writes go through hubFetch, and the only routes named are the three updates routes",
          set(re.findall(r'hubFetch\("(/api/hub/[a-z/]+)"', UPD_JS)) == {"/api/hub/updates", "/api/hub/updates/check", "/api/hub/updates/dismiss"})
    check("hubFetch sends the X-Jarvis-Hub header on every POST", '"X-Jarvis-Hub": "1"' in JS)
    check("Ignore this version asks first (it cannot be undone from the tab)", 'askFirst("Ignore this version?"' in UPD_JS)
    check("N23d: there is no Update / Upgrade / Install button anywhere in the tab",
          not re.search(r'text: "(Update|Upgrade|Install)[^"]*"', UPD_JS) and "Update</button>" not in HTML.split("updatesPane")[1].split("historyPane")[0])
    check("N22 part d: the tab has no source filter and no chips, selects or search box",
          not re.search(r"<select|<input|class=\"chip|type=\"search", HTML.split('id="updatesPane"')[1].split('id="historyPane"')[0]) and "chip" not in UPD_JS
          # the code only: a comment may say "no source filter", and Array.filter() is not a filter control
          and "filter" not in re.sub(r"//[^\n]*", "", UPD_JS).lower().replace(".filter(", "("))
    check("N23c: badge only. The tab sends no notification of its own: toast() is used for answers to the person's own button presses only",
          "host.notify" not in UPD_JS and "notify(" not in UPD_JS.replace("toast(", ""))
    check("the hub side never mentions a notification for findings (updates.py and updates_admin.py)",
          not re.search(r"notify|notice", (ROOT / "jarvis-cli" / "jarvis" / "hub" / "updates.py").read_text(encoding="utf-8").lower())
          and "notify" not in (ROOT / "jarvis-cli" / "jarvis" / "hub" / "updates_admin.py").read_text(encoding="utf-8").lower())
    check("the badge is cleared when the panel closes (the timer is stopped)", "clearTimeout(S.updTimer)" in JS)
    check("the styles exist for every class the tab draws", all(("." + c) in CSS for c in ("upd__group", "upd__head", "upd__name", "upd__n", "upd__health", "upd__row", "upd__main", "upd__title", "upd__ver", "upd__meta", "upd__note", "upd__spacer")))
    used = set(re.findall(r'cls: "(upd__[a-z_]+)', UPD_JS))
    check("...and every upd__ class the code uses is styled", used and all(("." + c) in CSS for c in used))


def test_settings_rows_and_owner_only():
    check("downloads-settings.js adds the two update rows to each provider block and exports the helper",
          "updateSettingRows(p, payload.update_settings)" in SETTINGS_JS and "updateSettingRows: updateSettingRows" in SETTINGS_JS)
    check("the rows are written with dl_provider_prefs (the hub's own validator and the confirmation stay the only ones)",
          "providerSettingArgs(p.id, f.key, v)" in SETTINGS_JS)
    # 9B made BOTH tools owner-only (dl_updates names the owner's installed software too), so the guest keeps only dl_list / dl_status.
    check("dl_updates_check and dl_updates are both owner-only (a guest's model never sees either; their handlers refuse a guest too)",
          "dl_updates_check" in tools.OWNER_ONLY_TOOLS and "dl_updates" in tools.OWNER_ONLY_TOOLS)


test_ops()
test_unattended()
test_inputs()
test_check_without_daemon()
test_dismiss_and_return()
test_provider_text_is_data()
test_settings_payload()
test_cli()
test_routes()
test_panel_files()
test_settings_rows_and_owner_only()

print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
