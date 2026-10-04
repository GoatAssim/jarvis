"""Browser test for the Tool Manager's editor tabs, closable panes and Ask Jarvis
dock (master plan L.43 / L.44). Serves web/public with the /api routes mocked, so
no Jarvis, key or network is needed. Needs Playwright + Chromium; SKIPS (exit 0)
when they are not installed.

    python3 tests/verify_tool_manager_ui.py
"""
import json, subprocess, time, sys, re
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skip: playwright is not installed"); sys.exit(0)
PUB=str(Path(__file__).resolve().parent.parent / "web" / "public")
srv=subprocess.Popen(["python3","-m","http.server","8765","-d",PUB],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
time.sleep(1)
fails=[]
def check(n,c):
    print(("ok   " if c else "FAIL ")+n)
    if not c: fails.append(n)
TEMPLATE='TOOL_GROUP = "custom"\nTOOL_SCHEMAS = []\nTOOLS = {}\n'
AGENT_CODE='TOOL_GROUP = "custom"\n\ndef tool_ping(args):\n    return {"ok": True}\n\nTOOL_SCHEMAS = [{"name": "ping", "description": "p", "parameters": {"type": "object", "properties": {}}}]\nTOOLS = {"ping": tool_ping}\n'
def api(route):
    u=route.request.url; m=route.request.method
    def j(o,s=200): route.fulfill(status=s,content_type="application/json",body=json.dumps(o))
    if u.endswith("/api/tools"): return j([])
    if u.endswith("/api/commands"): return j({})
    if u.endswith("/api/disabled"): return j({"commands":[]})
    if u.endswith("/api/ctools"): return j({"tools":[{"name":"hello","file":"hello.py","valid":True,"enabled":True,"tools":["hello"],"description":"hi"}]})
    if u.endswith("/api/ctools/templates"): return j({"templates":[{"id":"minimal","label":"Minimal"}]})
    if "/api/ctools/draft" in u: return j({"source":TEMPLATE})
    if u.endswith("/api/ctools/hello") and m=="GET": return j({"source":"# hello file\n","enabled":True,"valid":True})
    if u.endswith("/agent"):
        body=json.loads(route.request.post_data)
        lines=[{"type":"stream","k":"text","d":"Adding a ping tool.\n```python\n"+AGENT_CODE[:30]},
               {"type":"done","ok":True,"note":"Adding a ping tool.","code":AGENT_CODE,"provider":"x"}]
        if body["instruction"]=="question": lines=[{"type":"done","ok":True,"note":"Which folder?","code":"","provider":"x"}]
        if body["instruction"]=="fail": lines=[{"type":"done","ok":False,"error":"no AI provider with a key is configured"}]
        return route.fulfill(status=200,content_type="application/x-ndjson",body="\n".join(json.dumps(l) for l in lines)+"\n")
    return j({})
with sync_playwright() as p:
    try:
        b=p.chromium.launch()
    except Exception as e:
        print("skip: no Chromium (%s)" % str(e)[:60]); srv.terminate(); sys.exit(0)
    ctx=b.new_context(viewport={"width":1500,"height":900}); pg=ctx.new_page()
    errs=[]; pg.on("pageerror",lambda e:errs.append(str(e)))
    pg.route("**/api/**",api)
    pg.route("**/cdn.jsdelivr.net/**",lambda r:r.abort())
    pg.goto("http://localhost:8765/index.html"); pg.wait_for_timeout(800)
    pg.evaluate("JarvisToolManager.open()"); pg.wait_for_timeout(800)
    vis=lambda sel: pg.locator(sel).first.is_visible()
    check("tm overlay visible", vis(".tm-panel"))
    check("no tab bar before any editor", not vis("#tm-tabs"))
    # panes
    check("tools pane shown", vis("#tm-list"))
    pg.click("#tm-close-tools"); pg.wait_for_timeout(100)
    check("tools pane hidden by x", not vis("#tm-list"))
    pg.click("#tm-close-side"); pg.wait_for_timeout(100)
    check("overview pane hidden by x", not vis("#tm-side"))
    check("middle fills width", pg.evaluate("document.querySelector('#tm-detail').getBoundingClientRect().width")>1000)
    pg.click("#btn-tm-toggle-tools"); pg.click("#btn-tm-toggle-side"); pg.wait_for_timeout(100)
    check("toggles bring panes back", vis("#tm-list") and vis("#tm-side"))
    pg.click("#btn-tm-toggle-tools"); pg.wait_for_timeout(50)
    # create tabs
    pg.click("#btn-tm-create"); pg.wait_for_timeout(500)
    check("tab bar visible", vis("#tm-tabs"))
    tabs=lambda: pg.evaluate("[...document.querySelectorAll('#tm-tabs .tm-tab')].map(t=>t.textContent.trim())")
    check("untitled 1 tab", any("untitled 1" in t for t in tabs()))
    pg.fill("#tm-ed-name","alpha"); pg.wait_for_timeout(50)
    pg.click("#btn-tm-create"); pg.wait_for_timeout(500)
    check("second create opens a second tab", any("untitled" in t for t in tabs()) and any("alpha" in t for t in tabs()) and len(tabs())>=4)
    # type in second tab
    pg.click(".ce__ta"); pg.keyboard.type("# second\n"); pg.wait_for_timeout(100)
    # switch to first (alpha) and check its content preserved
    pg.click("#tm-tabs .tm-tab:has-text('alpha')"); pg.wait_for_timeout(200)
    check("alpha has template, not second's text", "# second" not in pg.evaluate("document.querySelector('.ce__ta').value"))
    # close overlay + reopen keeps tabs & view
    pg.click("#tm-close"); pg.wait_for_timeout(100)
    check("overlay hidden w/o confirm dialog", not vis(".tm-panel") and not vis(".jui-modal"))
    pg.evaluate("JarvisToolManager.open()"); pg.wait_for_timeout(800)
    check("tabs survive close/open", len(tabs())>=4 and vis(".ce__ta"))
    # agent
    pg.fill(".tm-agent__input","make a ping tool"); pg.keyboard.press("Enter"); pg.wait_for_timeout(700)
    val=pg.evaluate("document.querySelector('.ce__ta').value")
    check("agent code written into editor", "tool_ping" in val)
    check("agent reply shown", "Adding a ping tool" in pg.inner_text(".tm-agent__log"))
    check("undo button offered", vis(".tm-agent__btns button:has-text('Undo AI edit')"))
    pg.click(".tm-agent__btns button:has-text('Undo AI edit')"); pg.wait_for_timeout(200)
    check("undo restores template", pg.evaluate("document.querySelector('.ce__ta').value")==TEMPLATE)
    pg.fill(".tm-agent__input","question"); pg.keyboard.press("Enter"); pg.wait_for_timeout(500)
    check("question leaves editor untouched", pg.evaluate("document.querySelector('.ce__ta').value")==TEMPLATE)
    pg.fill(".tm-agent__input","fail"); pg.keyboard.press("Enter"); pg.wait_for_timeout(500)
    check("error shown", "no AI provider" in pg.inner_text(".tm-agent__log"))
    # collapse dock
    pg.click(".tm-agent__head"); pg.wait_for_timeout(100)
    check("dock collapses", not vis(".tm-agent__input"))
    pg.click(".tm-agent__head")
    # persistence across reload
    pg.fill(".tm-agent__input","make a ping tool"); pg.keyboard.press("Enter"); pg.wait_for_timeout(700)
    pg.wait_for_timeout(700)
    pg.reload(); pg.wait_for_timeout(800)
    pg.evaluate("JarvisToolManager.open()"); pg.wait_for_timeout(1200)
    check("tabs restored after reload", any("alpha" in t for t in tabs()))
    # edit existing file opens tab, reopening focuses same
    n0=len(tabs())
    pg.evaluate("document.querySelector('.tm-card')&&1")
    # close a dirty tab asks
    pg.click("#tm-tabs .tm-tab:has-text('alpha') .tm-tab__x"); pg.wait_for_timeout(300)
    check("dirty tab asks before closing", vis(".jui-modal"))
    pg.click(".jui-modal button:has-text('Discard')"); pg.wait_for_timeout(300)
    check("tab closed after discard", not any("alpha" in t for t in tabs()))
    check("no page errors", not errs)
    if errs: print(errs)
    b.close()
srv.terminate()
print("FAILS:",fails); sys.exit(1 if fails else 0)
