"""Browser test for the Tool Manager's SCREEN-FILE TABS and their server-side backup
(master plan: "tool folders" -- the tabs had no browser test, and unsaved screen text was
only kept in the browser). Serves web/public with the /api routes mocked, so no Jarvis, key
or network is needed. Needs Playwright + Chromium; SKIPS (exit 0) when they are not installed.

It opens a saved tool that has a screen folder (tool.html + tool.js) and checks:
  - the file bar lists the .py and the screen files; opening one loads its text;
  - typing in a screen file marks its tab (and the editor tab) as unsaved, and the text
    survives switching tabs;
  - ~3 s after the last keystroke the draft is PUT with the .py AND the unsaved screen text
    ({"web": {...}}), and only the changed file is in it;
  - saving the screen file PUTs it to /file and, with nothing else unsaved, DELETEs the draft;
  - with the .py AND a screen file changed, saving only the .py leaves the screen text in
    the draft (it is PUT again after the save deleted it);
  - "Save all" appears for two unsaved files;
  - Unfinished tools lists a draft with "+ 2 screen files", and Restore puts that text back
    into the tabs, unsaved.

    python3 tests/verify_tool_folders_tabs_ui.py

Written, not run, by the author of the patch.
"""
import json, subprocess, sys, time
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skip: playwright is not installed"); sys.exit(0)

PUB = str(Path(__file__).resolve().parent.parent / "web" / "public")
srv = subprocess.Popen(["python3", "-m", "http.server", "8766", "-d", PUB], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
fails = []
def check(n, c, detail=None):
    print(("ok   " if c else "FAIL ") + n + ("" if c or detail is None else "  -> " + str(detail)[:300]))
    if not c: fails.append(n)

PY = ('TOOL_GROUP = "custom"\n\ndef tool_pagey(args):\n    return {"ok": True}\n\n'
      'TOOL_SCHEMAS = [{"name": "pagey", "description": "p", "parameters": {"type": "object", "properties": {}}}]\n'
      'TOOLS = {"pagey": tool_pagey}\nTOOL_UI = [{"id": "pagey_ui", "label": "Pagey", "mode": "button", "path": "pagey_ui"}]\n')
FILES = {"pagey_ui/tool.html": "<p>saved html</p>", "pagey_ui/tool.js": "// saved js"}
calls = {"draft_put": [], "draft_delete": [], "file_put": []}
drafts_listed = []                                    # what GET /api/ctools/drafts returns (set by the test)
draft_bodies = {}                                     # key -> the last PUT body

def api(route):
    req = route.request; u = req.url.split("?")[0]; m = req.method
    def j(o, s=200): route.fulfill(status=s, content_type="application/json", body=json.dumps(o))
    if u.endswith("/api/tools"): return j([])
    if u.endswith("/api/commands"): return j({})
    if u.endswith("/api/disabled"): return j({"commands": []})
    if u.endswith("/api/tool-commands"): return j({"commands": []})
    if u.endswith("/api/ctools"):
        return j({"tools": [{"name": "pagey", "file": "pagey.py", "valid": True, "enabled": True, "tools": ["pagey"], "description": "has a screen"}]})
    if u.endswith("/api/ctools/templates"): return j({"templates": [{"id": "minimal", "label": "Minimal"}]})
    if u.endswith("/api/ctools/drafts") and m == "GET": return j({"drafts": drafts_listed})
    if "/api/ctools/drafts/" in u:
        key = u.rsplit("/", 1)[1]
        if m == "PUT":
            body = json.loads(req.post_data or "{}"); calls["draft_put"].append(body); draft_bodies[key] = body
            return j({"ok": True, "key": key})
        if m == "DELETE":
            calls["draft_delete"].append(key); return j({"ok": True})
        if m == "GET":
            b = draft_bodies.get(key) or {}
            return j({"ok": True, "key": key, "source": b.get("source", PY), "name": "pagey", "orig_name": "pagey", "mode": "edit",
                      "web": b.get("web", {}), "file_newer": False, "versions": 0})
    if u.endswith("/api/ctools/pagey/files"):
        return j({"web": {"files": [{"rel": r, "kind": r.rsplit(".", 1)[1], "exists": True, "label": "Pagey"} for r in FILES]}})
    if u.endswith("/api/ctools/pagey/file"):
        if m == "PUT":
            body = json.loads(req.post_data or "{}"); calls["file_put"].append(body); FILES[body["path"]] = body["text"]
            return j({"ok": True})
        rel = req.url.split("path=")[1].split("&")[0].replace("%2F", "/")
        return j({"text": FILES.get(rel, ""), "exists": rel in FILES})
    if u.endswith("/api/ctools/pagey") and m == "GET": return j({"source": PY, "enabled": True, "valid": True})
    if u.endswith("/api/ctools/pagey") and m in ("PUT", "POST"): return j({"ok": True, "name": "pagey", "tools": ["pagey"], "ui": []})
    return j({})

with sync_playwright() as p:
    try:
        b = p.chromium.launch()
    except Exception as e:
        print("skip: no Chromium (%s)" % str(e)[:60]); srv.terminate(); sys.exit(0)
    ctx = b.new_context(viewport={"width": 1500, "height": 900}); pg = ctx.new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/api/**", api)
    pg.route("**/cdn.jsdelivr.net/**", lambda r: r.abort())
    pg.goto("http://localhost:8766/index.html"); pg.wait_for_timeout(800)
    pg.evaluate("JarvisToolManager.open()"); pg.wait_for_timeout(800)
    vis = lambda sel: pg.locator(sel).first.is_visible()
    ftabs = lambda: pg.evaluate("[...document.querySelectorAll('.tm-ftab')].map(t => t.textContent.trim())")
    cls = lambda label: pg.evaluate("(l) => { const t = [...document.querySelectorAll('.tm-ftab')].find(x => x.textContent.includes(l)); return t ? t.className : ''; }", label)

    # --- open the saved tool for editing ----------------------------------------------------
    pg.click("#tm-list >> text=pagey"); pg.wait_for_timeout(300)
    pg.click("#tm-detail button:has-text('Edit')"); pg.wait_for_timeout(900)
    check("the editor is open on the tool", vis(".ce__ta") or vis(".tm-ed"))
    names = ftabs()
    check("the file bar lists the .py and both screen files", len(names) == 3 and any("tool.html" in n for n in names) and any("tool.js" in n for n in names), names)

    # --- edit a screen file ------------------------------------------------------------------
    pg.click(".tm-ftab:has-text('tool.html')"); pg.wait_for_timeout(500)
    check("opening a screen tab marks it active", "is-active" in cls("tool.html"))
    check("... and shows the saved text", "saved html" in pg.inner_text("body"))
    pg.keyboard.press("Control+End"); pg.keyboard.type(" EDITED"); pg.wait_for_timeout(200)
    check("typing marks the screen tab unsaved", "is-dirty" in cls("tool.html"))
    check("... and the other screen tab is not", "is-dirty" not in cls("tool.js"))
    pg.click(".tm-ftab:has-text('tool.js')"); pg.wait_for_timeout(300)
    pg.click(".tm-ftab:has-text('tool.html')"); pg.wait_for_timeout(300)
    check("the typed text survives switching tabs", "EDITED" in pg.inner_text("body"))

    # --- the backup carries the screen text --------------------------------------------------
    n_before = len(calls["draft_put"])
    pg.wait_for_timeout(3600)
    check("a draft was PUT after the pause", len(calls["draft_put"]) > n_before, calls["draft_put"][-1:] )
    last = calls["draft_put"][-1] if calls["draft_put"] else {}
    check("... with the .py", last.get("source") == PY and last.get("mode") == "edit" and last.get("origName") == "pagey", {k: last.get(k) for k in ("mode", "origName")})
    web = last.get("web") or {}
    check("... and only the changed screen file's text", list(web) == ["pagey_ui/tool.html"] and web["pagey_ui/tool.html"].endswith("EDITED"), web)
    key = None
    for k in draft_bodies: key = k
    check("... under one draft key", key is not None)

    # --- saving the screen file removes the backup when nothing else is unsaved -------------
    d_before = len(calls["draft_delete"])
    pg.keyboard.press("Control+s"); pg.wait_for_timeout(700)
    check("Ctrl+S on the screen tab PUTs that file", calls["file_put"] and calls["file_put"][-1]["path"] == "pagey_ui/tool.html" and calls["file_put"][-1]["text"].endswith("EDITED"), calls["file_put"][-1:])
    check("... and the tab is no longer unsaved", "is-dirty" not in cls("tool.html"))
    pg.wait_for_timeout(3600)
    check("... so the draft is deleted (nothing is unfinished)", len(calls["draft_delete"]) > d_before and calls["draft_delete"][-1] == key, calls["draft_delete"])

    # --- the .py and a screen file both unsaved; saving the .py keeps the screen text -------
    pg.click(".tm-ftab:has-text('tool.js')"); pg.wait_for_timeout(300)
    pg.keyboard.press("Control+End"); pg.keyboard.type(" JS2"); pg.wait_for_timeout(200)
    pg.click(".tm-ftab:has-text('tool.py')"); pg.wait_for_timeout(300)
    pg.keyboard.press("Control+End"); pg.keyboard.type("\n# py2\n"); pg.wait_for_timeout(300)
    check("Save all is offered for two unsaved files", vis("#tm-ed-saveall"))
    n_before = len(calls["draft_put"])
    pg.wait_for_timeout(3600)
    last = calls["draft_put"][-1] if len(calls["draft_put"]) > n_before else {}
    check("the draft holds the edited .py and the unsaved .js", "# py2" in last.get("source", "") and list((last.get("web") or {})) == ["pagey_ui/tool.js"], last.get("web"))
    n_before = len(calls["draft_put"])
    pg.click("#tm-ed-save"); pg.wait_for_timeout(900)                       # saves the .py only
    check("saving the .py leaves the .js tab unsaved", "is-dirty" in cls("tool.js"))
    pg.wait_for_timeout(3600)
    again = calls["draft_put"][n_before:]
    check("... and the draft is written again with that screen text (the save had removed it)", again and (again[-1].get("web") or {}).get("pagey_ui/tool.js", "").endswith("JS2"), again[-1:] )

    # --- Unfinished tools and Restore --------------------------------------------------------
    drafts_listed.append({"key": key, "name": "pagey", "orig_name": "pagey", "mode": "edit", "chars": 400, "lines": 12, "versions": 0,
                          "saved": "2026-10-12T10:00:00", "file_newer": False, "web_files": ["pagey_ui/tool.html", "pagey_ui/tool.js"]})
    draft_bodies[key] = {"source": PY + "# restored\n", "web": {"pagey_ui/tool.html": "<p>restored html</p>", "pagey_ui/tool.js": "// restored js"}}
    pg.click("#tm-tabs .tm-tab:has-text('pagey') .tm-tab__x"); pg.wait_for_timeout(300)
    if vis(".jui-modal"):
        pg.click(".jui-modal button:has-text('Discard')"); pg.wait_for_timeout(300)
    pg.evaluate("JarvisToolManager.open()"); pg.wait_for_timeout(500)
    pg.reload(); pg.wait_for_timeout(800); pg.evaluate("JarvisToolManager.open()"); pg.wait_for_timeout(900)
    side = pg.inner_text("#tm-side")
    check("Unfinished tools says the draft holds 2 screen files", "2 screen files" in side, side[:300])
    pg.click("#tm-side button:has-text('Restore')"); pg.wait_for_timeout(1200)
    check("Restore opens the tool with its screen tabs", len(ftabs()) == 3, ftabs())
    check("... and both restored screen tabs are unsaved", "is-dirty" in cls("tool.html") and "is-dirty" in cls("tool.js"), (cls("tool.html"), cls("tool.js")))
    pg.click(".tm-ftab:has-text('tool.html')"); pg.wait_for_timeout(400)
    check("... with the backed-up text", "restored html" in pg.inner_text("body"))
    n_before = len(calls["draft_put"])
    pg.wait_for_timeout(3600)
    check("restoring does not wipe the backup's screen text on the server", all((c.get("web") or {}) for c in calls["draft_put"][n_before:]), calls["draft_put"][n_before:])

    check("no page errors", not errs, errs)
    if errs: print(errs)
    b.close()
srv.terminate()
print("\n%d failed" % len(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
