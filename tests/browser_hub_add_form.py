#!/usr/bin/env python3
"""Browser test for the reworked "Add a download" menu of the Downloads panel (9C).

Tested against a FAKE hub: the real tool.html / tool.css / tool.js are mounted in a shadow root of a bare page in headless Chromium (the
way tool-ui mounts them), with a host whose runTool() and fetch() are scripted in the page.  No server, no network, no hub, no
yt-dlp.  The existing tests/browser_hub_panel.py runs the real web/server.js instead; this one needs nothing but Playwright.

Run:  python tests/browser_hub_add_form.py            (prints "skipped" and exits 0 when Playwright or its Chromium is missing)
      HUB_SHOTS=/some/dir python tests/browser_hub_add_form.py     also saves screenshots there (dark and light, wide and narrow)

Plain asserts, no network.  Nothing here has run on Windows or in the owner's browser.
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "jarvis-cli" / "jarvis" / "actions" / "hub_ui"
SHOTS = os.environ.get("HUB_SHOTS", "")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name + ("  -> " + str(extra) if extra != "" else ""))


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skipped: playwright is not installed")
    sys.exit(0)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html, body { margin: 0; height: 100%; background: var(--bg-1); color: var(--text); font-family: system-ui, sans-serif; }
#h { height: 100vh; display: flex; flex-direction: column; }
</style></head><body><div id="h"></div></body></html>"""

THEMES = {
    "dark": {"--bg-1": "#0b0f14", "--bg-panel": "#121820", "--bg-panel-2": "#161d26", "--border": "#26303b", "--border-strong": "#3a4856", "--text": "#dde3e8",
             "--text-dim": "#9aa4ad", "--text-dimmer": "#8793a0", "--accent": "#38bdf8", "--accent-soft": "#7dd3fc", "--green": "#4ade80", "--orange": "#fb923c", "--red": "#f87171"},
    "light": {"--bg-1": "#ffffff", "--bg-panel": "#f4f6f8", "--bg-panel-2": "#eef1f4", "--border": "#cfd6dd", "--border-strong": "#aab4be", "--text": "#16202a",
              "--text-dim": "#46525e", "--text-dimmer": "#5d6a77", "--accent": "#0b6cb0", "--accent-soft": "#0b5a94", "--green": "#137333", "--orange": "#9a4a00", "--red": "#b42318"},
}

# The fake hub, in the page.  window.__cfg.override(name, args) may answer a call; return undefined to fall through to the defaults.
BOOT = """
async ([css, html, js, themes]) => {
  window.__calls = []; window.__notes = []; window.__fetches = []; window.__seq = 0;
  window.__cfg = { delay: 0, override: null };
  window.EventSource = undefined;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const classify = (u) => /bad/.test(u) ? { input: u, ok: false, problem: "private address" }
    : /^magnet:/.test(u) ? { input: u, ok: true, engine: "stremio", reason: "magnet link (Stremio engine)", add_tool: "dl_add_stremio" }
    : /youtu/.test(u) ? { input: u, ok: true, engine: "ytdlp", reason: "YouTube page (yt-dlp engine)", add_tool: "dl_add_ytdlp" }
    : { input: u, ok: true, engine: "http", reason: "direct link (HTTP engine)", add_tool: "dl_add" };
  const defaults = (name, args) => {
    if (name === "dl_list") return { ok: true, items: [], summary: { library_root: "/downloads", notify_options: { actions: [{ id: "discord", label: "Discord" }, { id: "inbox", label: "Notifications inbox" }] } } };
    if (name === "dl_add" && args.dry_run) return { ok: true, dry_run: true, decisions: (args.urls || []).map(classify) };
    if (name === "dl_add" || name === "dl_add_ytdlp") {
      const urls = args.urls || [];
      const good = urls.filter((u) => !/bad/.test(u));
      const skipped = urls.filter((u) => /bad/.test(u)).map((u) => ({ input: u, problem: "private address" }));
      if (!good.length) return { ok: false, error: "private address", skipped };
      return { ok: true, count: good.length, added: good.map((u) => ({ id: "h_" + (++window.__seq), duplicate: false, start_at: args.start_at ? "2026-10-28T03:00:00" : null })), skipped };
    }
    if (name === "dl_add_stremio") return { ok: true, count: 1, added: [{ id: "h_" + (++window.__seq), duplicate: false }], skipped: [] };
    return { ok: true };
  };
  const host = {
    root: null,
    storage: { get: async () => null, set: async () => {} },
    notify: (m, level) => { window.__notes.push({ m: String(m), level }); },
    onClose: () => {},
    runTool: async (name, args) => {
      window.__calls.push({ name, args: JSON.parse(JSON.stringify(args || {})) });
      if (window.__cfg.delay && !(args && args.dry_run) && /^dl_add/.test(name)) await sleep(window.__cfg.delay);
      const o = window.__cfg.override ? window.__cfg.override(name, args || {}) : undefined;
      return o !== undefined ? o : defaults(name, args || {});
    },
  };
  window.fetch = async (path, opt) => {
    window.__fetches.push({ path: String(path), body: opt && opt.body ? String(opt.body) : "" });
    const files = { ok: true, name: "Some torrent", files: [{ index: 0, path: "a.mkv", size: 1000 }, { index: 1, path: "b.mkv", size: 2000 }] };
    const ok = /torrent\\/files/.test(path);
    return { ok: ok, status: ok ? 200 : 404, json: async () => (ok ? files : { ok: false, error: "fake" }) };
  };
  window.__setTheme = (name) => { Object.entries(themes[name]).forEach(([k, v]) => document.documentElement.style.setProperty(k, v)); };
  window.__setTheme("dark");
  const h = document.getElementById("h");
  const root = h.attachShadow({ mode: "open" });
  host.root = root;
  root.innerHTML = "<style>" + css + "</style><div class='tool-ui-content'>" + html + "</div>";
  window.__root = () => root;
  window.__q = (s) => root.querySelector(s);
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  await new AsyncFunction("host", js)(host);
  // contrast helpers
  const rgba = (c) => { const cv = document.createElement("canvas"); cv.width = cv.height = 1; const x = cv.getContext("2d"); x.clearRect(0, 0, 1, 1); x.fillStyle = "#000"; x.fillStyle = c; x.fillRect(0, 0, 1, 1); const d = x.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  window.__contrast = (sel) => {
    const el = root.querySelector(sel);
    if (!el) return -1;
    const layers = [];
    let n = el;
    while (n) {
      if (n.nodeType === 1) { const c = rgba(getComputedStyle(n).backgroundColor); if (c[3] > 0) layers.push(c); if (c[3] >= 0.99) break; }
      n = n.parentNode; if (n && n.nodeType === 11) n = n.host;
    }
    let base = rgba(getComputedStyle(document.body).backgroundColor).slice(0, 3);
    layers.reverse().forEach((c) => { base = base.map((v, i) => Math.round(c[i] * c[3] + v * (1 - c[3]))); });
    const fg = rgba(getComputedStyle(el).color);
    const fgc = fg.slice(0, 3).map((v, i) => Math.round(v * fg[3] + base[i] * (1 - fg[3])));
    const a = lum(fgc), b = lum(base);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
}
"""


def main():
    css = (UI / "tool.css").read_text(encoding="utf-8")
    html = (UI / "tool.html").read_text(encoding="utf-8")
    js = (UI / "tool.js").read_text(encoding="utf-8")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser installed
            print("skipped: Chromium could not start (" + str(exc).splitlines()[0][:100] + ")")
            return
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "fake" not in m.text else None)

        def fresh(width=1280, height=900):
            page.set_viewport_size({"width": width, "height": height})
            page.set_content(PAGE)
            page.evaluate(BOOT, [css, html, js, THEMES])
            page.wait_for_function("window.__q('#addOpen') && window.__q('#addOpen').options.length > 1")

        def ev(expr, arg=None):
            return page.evaluate(expr, arg) if arg is not None else page.evaluate(expr)

        def text(sel):
            return ev("(s) => { const e = window.__q(s); return e ? e.textContent : null; }", sel)

        def hidden(sel):
            return ev("(s) => { const e = window.__q(s); return !e || e.hidden || e.getBoundingClientRect().width === 0; }", sel)

        def calls(name=None, dry=None):
            out = ev("window.__calls")
            out = [c for c in out if (name is None or c["name"] == name)]
            if dry is not None:
                out = [c for c in out if bool(c["args"].get("dry_run")) == dry]
            return out

        def adds():
            return [c for c in ev("window.__calls") if c["name"].startswith("dl_add") and not c["args"].get("dry_run")]

        def override(js_fn):
            ev("(f) => { window.__cfg.override = eval(f); }", js_fn)

        def paste(links):
            ev("window.__q('#addHint').textContent = ''")      # so ready() cannot match the line of the last paste
            page.fill("#addInput", "\n".join(links))

        def ready(expect):
            page.wait_for_function("(t) => window.__q('#addHint').textContent.indexOf(t) === 0", arg=expect, timeout=5000)

        def shot(name):
            if SHOTS:
                os.makedirs(SHOTS, exist_ok=True)
                page.screenshot(path=os.path.join(SHOTS, name + ".png"))

        FILE = "https://a.example/x.zip"
        V1, V2 = "https://youtu.be/aaa", "https://youtu.be/bbb"
        MAG = "magnet:?xt=urn:btih:" + "0" * 40

        # ---- 1. first look
        fresh()
        check("the form is the first thing in the middle pane", not hidden("#addForm") and not hidden("#addInput"))
        check("the five option groups are there and all closed", ev("['addWhere','addWhen','addSpeed','addTell','addBatch'].every((i) => window.__q('#' + i) && !window.__q('#' + i).open)"))
        check("a closed group still says what it holds: the usual words", text("#addStateWhere") == "usual folder" and text("#addStateWhen") == "right away"
              and text("#addStateSpeed") == "no limit of its own" and text("#addStateTell") == "my notification settings" and text("#addStateBatch") == "none")
        check("the torrent row, the video block and the result are not shown until a link needs them", hidden("#addTorrent") and hidden("#addVideo") and hidden("#addResult") and hidden("#addLinks"))
        check("the button says what it does with nothing pasted", text("#addBtn") == "Add to downloads")
        check("the notification choices were built from what the hub sent", ev("window.__q('#addChannels').querySelectorAll('input').length") == 2)
        shot("01-dark-wide-empty")

        # ---- 2. the main path: three links, one sentence, who takes each
        paste([V1, V2, MAG])
        ready("3 links")
        check("the sentence says what each link is and which engine takes it", text("#addHint") == "3 links: 2 video pages (yt-dlp), 1 magnet (Stremio)", text("#addHint"))
        check("a screen reader gets the same in words, in a live region", text("#addHintSr") == "3 links: 2 video pages taken by yt-dlp, 1 magnet taken by Stremio"
              and ev("window.__q('#addHintSr').getAttribute('aria-live')") == "polite")
        check("each link has its own row: Ready, the link, what it is, the engine and the router's reason (without its engine tail)",
              ev("window.__q('#addLinks').children.length") == 3 and "Ready" in text("#addLinks li:first-child .af-link__tag")
              and text("#addLinks li:first-child .af-link__what") == "video page \u00B7 yt-dlp \u2014 YouTube page", text("#addLinks li:first-child .af-link__what"))
        check("the video choices appear because a video page is in the list; the groups stay closed", not hidden("#addVideo") and ev("!window.__q('#addWhere').open"))
        check("a torrent among other links: no 'Choose files' button, but the row says how to choose", not hidden("#addTorrent") and hidden("#addChoose") and "paste just that one torrent's link" in text("#addTorrentHelp"), text("#addTorrentHelp"))
        check("the button counts the links", text("#addBtn") == "Add 3 downloads")
        shot("02-dark-wide-three-links")

        # ---- 3. a problem named per link
        paste([FILE, "https://a.example/bad.zip"])
        ready("2 links")
        check("a problem is named in the sentence and on its row", text("#addHint") == "2 links: 1 direct file (HTTP) \u00B7 1 can't be added"
              and ev("window.__q('#addHint').classList.contains('bad')") and "Can't add" in text("#addLinks li:nth-child(2) .af-link__tag")
              and text("#addLinks li:nth-child(2) .af-link__what") == "private address")
        check("with no video page the video block goes away again", hidden("#addVideo"))
        paste([FILE])
        ready("1 link")
        check("one link: the sentence names its kind and reason, no list", text("#addHint") == "1 link: direct file (HTTP) \u2014 direct link" and hidden("#addLinks"), text("#addHint"))
        paste(["hello https://a.example/x.zip world"])
        ready("1 link")
        check("text that isn't a link is named, not silently dropped", "2 pieces of text that aren't links are ignored" in text("#addHint"), text("#addHint"))

        # ---- 4. one magnet: choose files
        paste([MAG])
        ready("1 link")
        check("one torrent: 'Choose files' is offered", not hidden("#addChoose") and "choose which to download" in text("#addTorrentHelp"))
        page.click("#addChoose")
        page.wait_for_function("window.__fetches.some((f) => /torrent\\/files/.test(f.path))")
        check("it opens the torrent's file list (asks the hub for it) and the form is out of the way", hidden("#addForm") and not hidden("#picker"), ev("window.__fetches.map((f) => f.path)"))

        # ---- 5. the video choices reach dl_add_ytdlp only; the dry run goes ten at a time
        fresh()
        paste([FILE, V1, V2, MAG])
        ready("4 links")
        page.select_option("#addVQuality", "720p")
        page.select_option("#addVContainer", "mkv")
        ev("() => { window.__q('#addVideoMore').open = true; }")
        page.check("#addVThumb")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addResult') && !window.__q('#addResult').hidden")
        got = {c["name"]: c["args"] for c in adds()}
        check("one call per engine's tool: dl_add, dl_add_ytdlp and dl_add_stremio", sorted(got) == ["dl_add", "dl_add_stremio", "dl_add_ytdlp"], sorted(got))
        check("the video choices went to dl_add_ytdlp", got["dl_add_ytdlp"].get("quality") == "720p" and got["dl_add_ytdlp"].get("container") == "mkv"
              and got["dl_add_ytdlp"].get("embed_thumbnail") is True and got["dl_add_ytdlp"].get("urls") == [V1, V2])
        check("and to no other tool (N4)", not any(k in got["dl_add"] or k in got["dl_add_stremio"] for k in ("quality", "container", "embed_thumbnail", "mode", "video_codec")))
        check("dl_add never got an engine-specific argument, and the magnet went to dl_add_stremio alone", got["dl_add"].get("urls") == [FILE] and got["dl_add_stremio"].get("magnet") == MAG and "engine" not in got["dl_add"])
        check("a fully added list: a plain headline, the box emptied, the choices reset", text("#addResult .af-result__head") == "Added 4 downloads" and ev("window.__q('#addInput').value") == ""
              and ev("window.__q('#addVQuality').value") == "best" and ev("window.__q('#addResult').classList.contains('is-ok')"))
        check("it says so in the corner too", any(n["m"] == "Added 4 downloads" for n in ev("window.__notes")), ev("window.__notes"))
        check("the focus is back in the box", ev("(window.__root().activeElement || {}).id") == "addInput")

        fresh()
        paste(["https://a.example/%d.zip" % i for i in range(23)])
        ready("23 links")
        sizes = [len(c["args"]["urls"]) for c in calls("dl_add", dry=True)]
        check("23 links are checked in three calls of ten, ten and three, and every one has an answer", sorted(sizes) == [3, 10, 10]
              and ev("window.__q('#addLinks').children.length") == 11 and "and 13 more" in text("#addLinks li:last-child"), (sizes, text("#addLinks li:last-child")))

        # ---- 6. audio mode shows its own rows and sends only what fits
        fresh()
        paste([V1])
        ready("1 link")
        page.select_option("#addVMode", "audio")
        check("audio mode hides the video rows and shows the audio ones", hidden("#addVQuality") and not hidden("#addVAudioFormat") and not hidden("#addVAudioQuality"))
        page.select_option("#addVAudioFormat", "flac")
        page.fill("#addVAudioQuality", "192K")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addResult') && !window.__q('#addResult').hidden")
        a = adds()[0]["args"]
        check("only the audio choices were sent", a.get("mode") == "audio" and a.get("audio_format") == "flac" and a.get("audio_quality") == "192K" and "quality" not in a and "container" not in a, a)

        # ---- 7. busy: no double submit
        fresh()
        ev("window.__cfg.delay = 500")
        paste([FILE])
        ready("1 link")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addForm').getAttribute('aria-busy') === 'true'")
        check("while adding: the button says so and is off, the form is locked and busy", text("#addBtn") == "Adding\u2026" and ev("window.__q('#addBtn').matches(':disabled')") and ev("window.__q('#addLock').disabled"))
        ev("() => { window.__q('#addForm').requestSubmit(); window.__q('#addForm').requestSubmit(); }")
        page.wait_for_function("window.__q('#addForm').getAttribute('aria-busy') === 'false'", timeout=5000)
        check("a second press, even from code, did nothing: one add call", len(adds()) == 1, len(adds()))
        check("afterwards the form works again", not ev("window.__q('#addBtn').matches(':disabled')") and not ev("window.__q('#addLock').disabled"))

        # ---- 8. partial success: some queued, one refused, one tool down; each with its reason; what was not added stays
        fresh()
        override("(name, args) => name === 'dl_add_ytdlp' && !args.dry_run ? { ok: false, error: \"yt-dlp isn't installed\" } : undefined")
        page.click("#addWhen > summary")
        page.fill("#addStart", "tomorrow at 3am")
        paste([FILE, "https://a.example/bad.zip", V1])
        ready("3 links")
        check("a start time makes it a scheduled add on the button", text("#addBtn") == "Schedule 3 downloads" and text("#addStateWhen") == "starts tomorrow at 3am")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addResult') && !window.__q('#addResult').hidden")
        check("partial: the headline counts what was added of what was sent", text("#addResult .af-result__head").startswith("Added 1 of 3 links"), text("#addResult .af-result__head"))
        whys = ev("[...window.__q('#addResult').querySelectorAll('.af-result__list li')].map((li) => li.textContent)")
        check("each refused link is shown with its own reason", len(whys) == 2 and any("private address" in w and "bad.zip" in w for w in whys) and any("yt-dlp isn't installed" in w and "youtu.be" in w for w in whys), whys)
        check("the refused links stay in the box, the added one is gone", sorted(ev("window.__q('#addInput').value").split("\n")) == sorted(["https://a.example/bad.zip", V1]))
        check("a partial add keeps the choices and opens the group that holds them", ev("window.__q('#addWhen').open") and ev("window.__q('#addStart').value") == "tomorrow at 3am")
        check("a partial add does not jump away to a row", not hidden("#addForm") and ev("window.__q('#addResult').classList.contains('is-partial')"))
        shot("03-dark-wide-partial")
        for theme in ("dark", "light"):
            ev("(t) => window.__setTheme(t)", theme)
            low = {s: ev("(s) => window.__contrast(s)", s) for s in (".af-sum", ".af-group__name", ".af-link__url", ".af-link__what", ".af-link.is-ok .af-link__tag", ".af-link.is-bad .af-link__tag", ".af-result__head", ".af-result__why")}
            check("readable in the " + theme + " theme (contrast of the add form's own text, at least 4.5)", all(v >= 4.5 or v == -1 for v in low.values()) and sum(1 for v in low.values() if v > 0) >= 5, low)
            shot("04-" + theme + "-wide-partial")
        ev("(t) => window.__setTheme(t)", "dark")

        # ---- 9. a refused write and a declined question
        fresh()
        override("(name, args) => /^dl_add/.test(name) && !args.dry_run ? { ok: false, error: 'The hub is read-only right now' } : undefined")
        paste([FILE])
        ready("1 link")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addResult') && !window.__q('#addResult').hidden")
        check("a refused write: nothing was added, the hub's reason, the link stays", text("#addResult .af-result__head") == "Nothing was added." and "The hub is read-only right now" in text("#addResult .af-result__list")
              and ev("window.__q('#addInput').value") == FILE and ev("window.__q('#addResult').classList.contains('is-bad')"))
        fresh()
        override("(name, args) => /^dl_add/.test(name) && !args.dry_run ? { ok: false, cancelled: true, error: 'not confirmed: nothing was queued' } : undefined")
        page.click("#addWhere > summary")
        page.fill("#addDest", "D:\\Games")
        check("a full-path folder warns at once that it will ask", not hidden("#addDestNote") and "confirm" in text("#addDestNote") and "asks first" in text("#addStateWhere"))
        paste([FILE])
        ready("1 link")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addResult') && !window.__q('#addResult').hidden")
        check("a declined confirmation is not an error: neutral, nothing added, the link stays", ev("window.__q('#addResult').classList.contains('is-info')") and "not confirmed" in text("#addResult .af-result__list")
              and ev("window.__q('#addInput').value") == FILE)

        # ---- 10. paused and scheduled
        fresh()
        page.click("#addWhen > summary")
        page.check("#addPaused")
        paste([FILE])
        ready("1 link")
        check("paused: the button says so", text("#addBtn") == "Add 1 download, paused" and text("#addStateWhen") == "added paused")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addResult') && !window.__q('#addResult').hidden")
        check("paused: the result says it waits for you", "paused: start it from the list when you are ready" in text("#addResult .af-result__head") and adds()[0]["args"].get("start_paused") is True, text("#addResult .af-result__head"))
        fresh()
        page.click("#addWhen > summary")
        page.fill("#addStart", "tomorrow at 3am")
        paste([FILE])
        ready("1 link")
        page.click("#addBtn")
        page.wait_for_function("window.__notes.length > 0")
        check("scheduled: start_at was sent as typed and the corner message says when it starts", adds()[0]["args"].get("start_at") == "tomorrow at 3am"
              and any(n["m"] == "Scheduled 1 download, will start at 2026-10-28 03:00" for n in ev("window.__notes")), ev("window.__notes"))

        # ---- 11. a question the hub asks back: a playlist
        fresh()
        pl = ("(name, args) => name === 'dl_add_ytdlp' && !args.dry_run && !args.playlist ? { ok: false, needs_clarification: true, queued: 0, message: 'That link is a playlist of 12 videos.',"
              " playlist: { title: 'T', count: 12, cap: 50 }, call: { tool: 'dl_add_ytdlp', url: args.urls[0], playlist: 'all' } } : undefined")
        override(pl)
        paste(["https://youtube.com/playlist?list=PL1"])
        ready("1 link")
        page.click("#addBtn")
        page.wait_for_function("window.__q('#addResult') && !window.__q('#addResult').hidden")
        check("a playlist question: nothing added yet, the hub's words, and the button", text("#addResult .af-result__head") == "Nothing was added yet: it needs your answer." and "playlist of 12" in text("#addResult")
              and text("#addResult button") == "Add the playlist (up to 50 videos)" and ev("window.__q('#addInput').value") == "https://youtube.com/playlist?list=PL1")
        page.click("#addResult button")
        page.wait_for_function("window.__q('#addResult .af-result__head') && window.__q('#addResult .af-result__head').textContent === 'Added 1 download'")
        last = adds()[-1]["args"]
        check("the button asked again with playlist=all for that one link, keeping the form's choices", last.get("playlist") == "all" and last.get("urls") == ["https://youtube.com/playlist?list=PL1"])
        check("then the box is empty", ev("window.__q('#addInput').value") == "")

        # ---- 12. keyboard
        fresh()
        paste([FILE])
        ready("1 link")
        page.focus("#addInput")
        page.keyboard.press("Tab")
        info = ev("() => { const a = window.__root().activeElement; const cs = getComputedStyle(a); return { tag: a.tagName, parent: a.parentElement && a.parentElement.id, outline: cs.outlineStyle, width: cs.outlineWidth }; }")
        check("Tab goes from the box straight to the first option group, with a visible focus ring", info["tag"] == "SUMMARY" and info["parent"] == "addWhere" and info["outline"] != "none" and info["width"] != "0px", info)
        page.keyboard.press("Enter")
        check("Enter opens the group", ev("window.__q('#addWhere').open"))
        page.keyboard.press("Enter")
        check("Enter closes it again", not ev("window.__q('#addWhere').open"))
        page.focus("#addInput")
        page.keyboard.press("Control+Enter")
        page.wait_for_function("window.__calls.some((c) => c.name === 'dl_add' && !c.args.dry_run)")
        check("Ctrl+Enter in the box adds", len(adds()) == 1)
        check("every control in the form has a name a screen reader can read", ev("""() => [...window.__root().querySelectorAll('#addForm input, #addForm select, #addForm textarea')]
            .every((e) => (e.labels && e.labels.length) || e.getAttribute('aria-label'))"""))
        check("the form never contains a raw-markup link: no img or script came out of text", ev("window.__root().querySelectorAll('#addForm img, #addForm script').length") == 0)

        # ---- 13. dropped links fill the form AND refresh the torrent row
        fresh()
        ev("""(m) => { const dt = new DataTransfer(); dt.setData('text/plain', m); const hub = window.__q('#hub');
            hub.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true })); }""", MAG)
        ready("1 link")
        check("dropped links go in the box and the whole form follows (the torrent row too)", ev("window.__q('#addInput').value") == MAG and not hidden("#addChoose") and text("#addBtn") == "Add 1 download")

        # ---- 14. text only: markup in a reason or a link is shown as text
        fresh()
        evil = '<img src=x onerror="window.__xss=1">'
        override("(name, args) => name === 'dl_add' && args.dry_run ? { ok: true, decisions: args.urls.map((u) => ({ ok: false, problem: '" + evil.replace("'", "") + "' })) } : undefined")
        paste(["https://a.example/" + evil.replace(" ", "%20").replace('"', "%22")])
        ready("1 link")
        check("a hostile reason is text, not markup", ev("window.__xss") is None and ev("window.__root().querySelectorAll('#addLinks img').length") == 0 and "<img" in text("#addLinks"))

        # ---- 15. narrow panes
        for width in (1280, 901, 420, 320):
            fresh(width, 900)
            paste([V1, V2, MAG, "https://a.example/bad.zip"] + ["https://a.example/%d.zip" % i for i in range(8)])
            ready("12 links")
            ev("() => { ['addWhere', 'addTell'].forEach((i) => { window.__q('#' + i).open = true; }); window.__q('#addVideoMore').open = true; }")
            over = ev("""() => { const out = []; ['#mid', '.body'].forEach((s) => { const e = window.__q(s); if (e && e.scrollWidth > e.clientWidth + 1) out.push(s + ' ' + e.scrollWidth + '>' + e.clientWidth); });
                const mid = window.__q('#mid').getBoundingClientRect(); const wide = [...window.__q('#addForm').querySelectorAll('*')].filter((e) => { const r = e.getBoundingClientRect();
                return r.width > 0 && (r.right > mid.right + 1 || r.left < mid.left - 1); }).slice(0, 4).map((e) => e.tagName + '#' + e.id + '.' + e.className); return { out, wide, mid: mid.width }; }""")
            check("no sideways scrolling and nothing sticking out of the middle pane at %dpx (pane %dpx wide)" % (width, over["mid"]), not over["out"] and not over["wide"], over)
            shot("05-dark-%d-twelve-links" % width)
        fresh(420, 900)
        ev("(t) => window.__setTheme(t)", "light")
        paste([V1, "https://a.example/bad.zip"])
        ready("2 links")
        shot("06-light-420")

        check("nothing threw or logged an error in the page", not errors, errors[:5])
        browser.close()
    print("\n%d passed, %d failed" % (passed, failed))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
