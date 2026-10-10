"""L.53 -- the Tool Maker's Ask Jarvis side panel, in a real browser, on its own.

verify_tool_manager_ui.py walks the panel once inside the Tool Manager's tab test. This file
is the L.53 check proper: the toggle needs an editor, each tool tab has its OWN conversation
(switching tabs swaps the thread, switching back finds it), the conversation is created tagged
the Tool Maker, its id is remembered per tab and forgotten by New chat (which asks first),
the reply is folded to a chip instead of the file, the editor is written and Undo restores it,
and the panel says plainly that it only edits. Serves web/public with the /api routes mocked
and the websocket replaced by a scripted fake (the panel speaks the main Ask panel's own
protocol), so no Jarvis, key or network is needed. Needs Playwright + Chromium; SKIPS (exit 0)
when they are not installed.

    python3 tests/verify_l53_tool_maker_ui.py
"""
import json, subprocess, time, sys, re
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skip: playwright is not installed"); sys.exit(0)
PUB=str(Path(__file__).resolve().parent.parent / "web" / "public")
srv=subprocess.Popen(["python3","-m","http.server","8766","-d",PUB],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
time.sleep(1)
fails=[]
created=[]
def check(n,c):
    print(("ok   " if c else "FAIL ")+n)
    if not c: fails.append(n)
TEMPLATE='TOOL_GROUP = "custom"\nTOOL_SCHEMAS = []\nTOOLS = {}\n'
AGENT_CODE='TOOL_GROUP = "custom"\n\ndef tool_ping(args):\n    return {"ok": True}\n\nTOOL_SCHEMAS = [{"name": "ping", "description": "p", "parameters": {"type": "object", "properties": {}}}]\nTOOLS = {"ping": tool_ping}\n'
FAKE_WS = """(() => {
  window.__asks = [];
  const reply = (text) => {
    const code = %s;
    if (text === 'make a ping tool') return [
      {type:'ask-start', cmdline:'jarvis'},
      {type:'ask-stream', ev:{k:'text', d:'Adding a ping tool.\\n```python\\n' + code.slice(0, 30)}},
      {type:'ask-stream', ev:{k:'text', d:code.slice(30) + '```'}},
      {type:'ask-stdout', line:'Jarvis: Adding a ping tool.'}, {type:'ask-stdout', line:'```python'},
      ...code.replace(/\\n$/, '').split('\\n').map((l) => ({type:'ask-stdout', line:l})),
      {type:'ask-stdout', line:'```'}, {type:'ask-exit', code:0}];
    if (text === 'question') return [{type:'ask-start'}, {type:'ask-stdout', line:'Jarvis: Which folder?'}, {type:'ask-exit', code:0}];
    return [{type:'ask-start'}, {type:'ask-error', message:'no AI provider with a key is configured'}];
  };
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this.l = {}; setTimeout(() => { this.readyState = 1; this._emit('open', {}); }, 5); }
    addEventListener(t, f) { (this.l[t] = this.l[t] || []).push(f); }
    removeEventListener() {}
    _emit(t, e) { (this.l[t] || []).forEach((f) => f(e)); if (this['on' + t]) this['on' + t](e); }
    send(s) {
      let m; try { m = JSON.parse(s); } catch (_) { return; }
      if (m.type !== 'ask') return;
      window.__asks.push(m);
      reply(m.text).forEach((o, i) => setTimeout(() => this._emit('message', {data: JSON.stringify(o)}), 20 + i * 5));
    }
    close() { this.readyState = 3; }
  }
  FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
  window.WebSocket = FakeWS;
})();""" % json.dumps(AGENT_CODE)
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
    if u.endswith("/api/conversations") and m=="POST":
        body=json.loads(route.request.post_data or "{}")
        created.append(body)
        return j({"id":"20260101-000000-%04d" % len(created)})
    if "/api/conversations/" in u and m=="GET": return j({"exchanges":[]})
    return j({})
with sync_playwright() as p:
    try:
        b=p.chromium.launch()
    except Exception as e:
        print("skip: no Chromium (%s)" % str(e)[:60]); srv.terminate(); sys.exit(0)
    ctx=b.new_context(viewport={"width":1500,"height":900}); pg=ctx.new_page()
    pg.add_init_script(FAKE_WS)
    errs=[]; pg.on("pageerror",lambda e:errs.append(str(e)))
    pg.route("**/api/**",api)
    pg.route("**/cdn.jsdelivr.net/**",lambda r:r.abort())
    pg.goto("http://localhost:8766/index.html"); pg.wait_for_timeout(800)
    pg.evaluate("JarvisToolManager.open()"); pg.wait_for_timeout(800)
    vis=lambda sel: pg.locator(sel).first.is_visible()
    chats=lambda: pg.evaluate("JSON.parse(localStorage.getItem('jarvis-tool-maker-chats')||'{}')")
    thread=lambda: pg.inner_text(".jae__thread")

    # 1. no editor, no panel
    check("Ask Jarvis is disabled until an editor is open", not pg.locator("#btn-tm-toggle-agent").is_enabled())
    check("the Overview is what shows first, the Ask panel is closed", vis("#tm-side") and not vis("#tm-ask"))

    # 2. two tool tabs: alpha and a fresh one
    pg.click("#btn-tm-create"); pg.wait_for_timeout(500)
    pg.fill("#tm-ed-name","alpha"); pg.wait_for_timeout(50)
    pg.click("#btn-tm-create"); pg.wait_for_timeout(500)
    check("with an editor open the toggle is enabled", pg.locator("#btn-tm-toggle-agent").is_enabled())
    pg.click("#btn-tm-toggle-agent"); pg.wait_for_timeout(250)
    check("the panel takes the Overview's slot", vis("#tm-ask") and not vis("#tm-side"))
    check("the slot is titled Ask Jarvis", pg.inner_text("#tm-side-title").strip()=="Ask Jarvis")
    check("the bar names the tool this conversation is about", pg.inner_text("#tm-ask-tool").strip()!="")
    check("the panel says plainly it only edits (it cannot run or save)",
          any(w in (pg.inner_text("#tm-ask")+pg.inner_text("#tm-ask-slot")).lower() for w in ("only edit","edit only","only edits","can only")))
    check("an empty tab starts with an empty thread and no Undo", "Adding a ping tool" not in thread() and not vis("#tm-ask-undo"))

    # 3. a turn in the fresh tab
    pg.fill(".jae__input","make a ping tool"); pg.keyboard.press("Enter"); pg.wait_for_timeout(1500)
    check("the reply is folded to a chip, not pasted into the thread",
          "Adding a ping tool" in thread() and "tool_ping" not in thread() and "Wrote the file" in thread())
    check("the file was typed into the editor", "tool_ping" in pg.evaluate("document.querySelector('.ce__ta').value"))
    check("the ask carried the tool's name and the editor's whole text",
          pg.evaluate("window.__asks.at(-1).toolMaker.source")==TEMPLATE and bool(pg.evaluate("window.__asks.at(-1).toolMaker.name !== undefined")))
    check("the conversation was created tagged Tool Maker", bool(created) and created[0].get("origin")=="tool-maker")
    check("its id is remembered under this tab only", len(chats())==1 and list(chats().keys())[0].startswith("tab:"))
    check("Undo AI edit is offered after an edit", vis("#tm-ask-undo"))

    # 4. one conversation per tool tab
    pg.click("#tm-tabs .tm-tab:has-text('alpha')"); pg.wait_for_timeout(300)
    check("another tab shows ITS OWN thread, not this one's", "Adding a ping tool" not in thread())
    check("...and the bar follows the tab", "alpha" in pg.inner_text("#tm-ask-tool"))
    pg.click("#tm-tabs .tm-tab:has-text('untitled')"); pg.wait_for_timeout(300)
    check("switching back finds the conversation as it was", "Adding a ping tool" in thread())

    # 5. Undo puts the old text back
    pg.click("#tm-ask-undo"); pg.wait_for_timeout(250)
    check("Undo restores the text from before the edit", pg.evaluate("document.querySelector('.ce__ta').value")==TEMPLATE)

    # 6. a question changes nothing in the editor
    pg.fill(".jae__input","question"); pg.keyboard.press("Enter"); pg.wait_for_timeout(800)
    check("a plain answer leaves the editor alone", pg.evaluate("document.querySelector('.ce__ta').value")==TEMPLATE)

    # 7. New chat asks first, then forgets this tab's conversation
    pg.click("#tm-ask-new"); pg.wait_for_timeout(250)
    check("New chat asks before it does anything", vis(".jui-modal"))
    pg.click(".jui-modal button:has-text('Cancel')"); pg.wait_for_timeout(200)
    check("Cancel leaves the conversation and its saved id", "Adding a ping tool" in thread() and len(chats())==1)
    pg.click("#tm-ask-new"); pg.wait_for_timeout(250)
    pg.click(".jui-modal button:has-text('Start new')"); pg.wait_for_timeout(400)
    check("Start new clears the thread", "Adding a ping tool" not in thread())
    check("...and forgets the saved id for this tab", len(chats())==0)

    # 8. toggling and closing
    pg.click("#btn-tm-toggle-agent"); pg.wait_for_timeout(150)
    check("toggled off: the Overview returns", vis("#tm-side") and not vis("#tm-ask"))
    pg.click("#btn-tm-toggle-agent"); pg.wait_for_timeout(150)
    pg.click("#tm-close-side"); pg.wait_for_timeout(100)
    check("the x hides the slot altogether", not vis("#tm-side") and not vis("#tm-ask"))
    pg.click("#btn-tm-toggle-side"); pg.wait_for_timeout(100)
    check("the Overview button brings it back", vis("#tm-side") and not vis("#tm-ask"))

    check("no page errors", not errs)
    if errs: print(errs)
    b.close()
srv.terminate()
print("FAILS:",fails); sys.exit(1 if fails else 0)
