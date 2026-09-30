// L.9 — the Run Sequence bar must be reachable.
//
// The bug: `.sequence-bar` (z-index 40, full width, bottom 18px) shared the
// bottom edge with the online pill (45) and the Logs / Notifications buttons
// (60), so those covered the bar — the Logs button hid about half of "Run
// Sequence" at desktop widths — and `.notifications-fab` had been silently
// turned into `position: relative` (sitting in page flow at the bottom-LEFT,
// off-screen on narrow viewports). The fix lifts everything that shares the
// strip above the bar while it is visible (body.has-sequence + a measured
// --seq-clear, written by app.js's syncSequenceClearance) and makes the
// Notifications button genuinely fixed again.
//
// This is a REAL-BROWSER test: overlap can't be checked without layout. It
// loads web/public/index.html and style.css into headless Chromium with every
// script blocked (so the app doesn't boot and no API is needed), un-hides the
// app, builds a sequence bar with the same markup renderSequenceBar() emits,
// and asks the browser which element receives a click at each control's centre
// (document.elementFromPoint) and how much of it is clickable.
//
// syncSequenceClearance() is NOT copied here: it is cut out of app.js by source
// range (same technique AGENTS.md describes for the other verify_*.js files),
// so this exercises the shipped function.
//
// Needs the `playwright` npm package and a Chromium it can launch. If either is
// missing the script prints SKIP and exits 0 — it never fails for lack of a
// browser, only for a real regression.
//
//   node tests/verify_l9_sequence_bar.js
//   PUBLIC_DIR=/path/to/other/web/public node tests/verify_l9_sequence_bar.js   # test another tree

const fs = require("fs");
const http = require("http");
const path = require("path");

let chromium;
try { ({ chromium } = require("playwright")); }
catch (e) { console.log("SKIP  playwright is not installed (npm i playwright) — L.9 layout checks not run"); process.exit(0); }

const PUBLIC = process.env.PUBLIC_DIR || path.join(__dirname, "..", "web", "public");
const MIME = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml" };

// --- the shipped function, cut out of app.js by source range -----------------
const appSrc = fs.readFileSync(path.join(PUBLIC, "app.js"), "utf8");
const a = appSrc.indexOf("let seqResizeObserver = null;");
const b = appSrc.indexOf("function renderSequenceBar()");
const SYNC_SRC = a >= 0 && b > a ? appSrc.slice(a, b) : null; // null on a pre-fix tree — the test then fails honestly

let passed = 0;
const failures = [];
function ok(name, cond, detail) {
  if (cond) { passed += 1; console.log(`ok       ${name}`); }
  else { failures.push(name); console.log(`FAILED   ${name}${detail ? "  — " + detail : ""}`); }
}

(async () => {
  const server = http.createServer((req, res) => {
    const rel = req.url.split("?")[0];
    const file = path.join(PUBLIC, rel === "/" ? "index.html" : rel);
    fs.readFile(file, (err, data) => {
      if (err) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream" });
      res.end(data);
    });
  });
  await new Promise((r) => server.listen(0, r));
  const url = `http://127.0.0.1:${server.address().port}/`;

  let browser;
  try { browser = await chromium.launch(); }
  catch (e) { console.log("SKIP  no launchable Chromium — L.9 layout checks not run (" + String(e.message).split("\n")[0] + ")"); server.close(); process.exit(0); }

  ok("syncSequenceClearance() exists in app.js", SYNC_SRC !== null);

  async function open(width, height) {
    const page = await browser.newPage({ viewport: { width, height } });
    await page.route("**/*.js", (r) => r.fulfill({ status: 200, contentType: "text/javascript", body: "" }));
    await page.goto(url);
    await page.evaluate((src) => {
      document.getElementById("boot").style.display = "none";
      document.getElementById("app").hidden = false;
      window.qs = (s) => document.querySelector(s);
      window.__sync = src ? new Function("qs", src + "; return syncSequenceClearance;")(window.qs) : function () {};
      // same markup renderSequenceBar() emits
      window.__setBar = (n) => {
        const bar = document.getElementById("sequence-bar"), items = document.getElementById("sequence-items");
        items.innerHTML = "";
        if (!n) { bar.hidden = true; window.__sync(); return; }
        bar.hidden = false;
        for (let i = 0; i < n; i++) {
          if (i) { const c = document.createElement("button"); c.className = "seq-connector"; c.textContent = "\u2192"; items.appendChild(c); }
          const d = document.createElement("div"); d.className = "seq-chip";
          d.innerHTML = `<span class="seq-chip__idx">${i + 1}</span><span>command-${i}</span><span class="seq-chip__x">\u00d7</span>`;
          items.appendChild(d);
        }
        window.__sync();
      };
      // hit-test helper: how much of `sel`'s box is actually clickable + who covers its centre
      window.__probe = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return { missing: true };
        const r = el.getBoundingClientRect();
        const mine = (e) => e && (e === el || el.contains(e));
        let n = 0, t = 0;
        for (let x = r.left + 3; x < r.right - 2; x += 3) for (let y = r.top + 3; y < r.bottom - 2; y += 3) {
          t += 1; if (mine(document.elementFromPoint(x, y))) n += 1;
        }
        const cover = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {
          rect: { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height },
          centre: mine(cover), coverId: mine(cover) ? null : (cover && (cover.id || cover.className || cover.tagName)),
          pct: t ? Math.round((100 * n) / t) : 0,
          inViewport: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight,
          position: getComputedStyle(el).position,
        };
      };
    }, SYNC_SRC);
    return page;
  }

  // ---- 1. every control reachable, at every width, with few and many chips ----
  const CONTROLS = {
    "Run Sequence": "#btn-seq-run",
    "Clear": "#btn-seq-clear",
    "first chip ×": ".seq-chip__x",
    "Logs button": "#btn-logs-fab",
    "Notifications button": "#btn-notifications-fab",
    "online pill": "#bottom-status-bar .status-pill",
  };
  for (const [w, h] of [[1500, 900], [1000, 800], [820, 800], [420, 800]]) {
    for (const chips of [1, 6]) {
      const page = await open(w, h);
      await page.evaluate((n) => window.__setBar(n), chips);
      for (const [label, sel] of Object.entries(CONTROLS)) {
        const p = await page.evaluate((s) => window.__probe(s), sel);
        const tag = `${w}px, ${chips} chip${chips > 1 ? "s" : ""}: ${label} is clickable at its centre and in view`;
        ok(tag, !p.missing && p.centre && p.inViewport, p.missing ? "element missing" : `covered by ${p.coverId}, inViewport=${p.inViewport}`);
      }
      // the two buttons the owner needs must be essentially fully exposed, not merely "centre ok"
      const run = await page.evaluate(() => window.__probe("#btn-seq-run"));
      ok(`${w}px, ${chips} chip${chips > 1 ? "s" : ""}: Run Sequence is fully exposed (no part covered)`, run.pct === 100, `only ${run.pct}% clickable`);
      await page.close();
    }
  }

  // ---- 2. Notifications button is really fixed, above Logs, same right edge ----
  {
    const page = await open(1500, 900);
    await page.evaluate(() => window.__setBar(0));
    const logs = await page.evaluate(() => window.__probe("#btn-logs-fab"));
    const nots = await page.evaluate(() => window.__probe("#btn-notifications-fab"));
    ok("Notifications button is position: fixed (not in page flow)", nots.position === "fixed", `position=${nots.position}`);
    ok("Notifications button is on-screen with no bar", nots.inViewport);
    ok("Notifications button sits above the Logs button", nots.rect.b <= logs.rect.t + 1, `nots.bottom=${nots.rect.b} logs.top=${logs.rect.t}`);
    ok("Notifications and Logs buttons share the right edge", Math.abs(nots.rect.r - logs.rect.r) <= 1, `${nots.rect.r} vs ${logs.rect.r}`);
    await page.close();
  }

  // ---- 3. hiding the bar puts everything back exactly where it was ----------
  {
    const page = await open(1500, 900);
    await page.evaluate(() => window.__setBar(0));
    const before = await page.evaluate(() => ({ l: window.__probe("#btn-logs-fab").rect, n: window.__probe("#btn-notifications-fab").rect, p: window.__probe("#bottom-status-bar .status-pill").rect }));
    await page.evaluate(() => window.__setBar(3));
    const lifted = await page.evaluate(() => window.__probe("#btn-logs-fab").rect);
    ok("with a bar, the Logs button is lifted above its resting position", lifted.t < before.l.t - 20, `${lifted.t} vs ${before.l.t}`);
    await page.evaluate(() => window.__setBar(0));
    const after = await page.evaluate(() => ({ l: window.__probe("#btn-logs-fab").rect, n: window.__probe("#btn-notifications-fab").rect, p: window.__probe("#bottom-status-bar .status-pill").rect,
      cls: document.body.classList.contains("has-sequence"), v: document.body.style.getPropertyValue("--seq-clear") }));
    ok("bar hidden again: body.has-sequence removed", after.cls === false);
    ok("bar hidden again: --seq-clear removed", after.v === "");
    ok("bar hidden again: Logs, Notifications and online pill are back at their resting positions",
      ["l", "n", "p"].every((k) => Math.abs(after[k].t - before[k].t) < 1 && Math.abs(after[k].l - before[k].l) < 1), JSON.stringify({ before, after }));
    await page.close();
  }

  // ---- 4. the lift tracks the bar when the bar changes height (ResizeObserver) ----
  {
    const page = await open(1000, 800);
    await page.evaluate(() => window.__setBar(3));
    const bar1 = await page.evaluate(() => document.getElementById("sequence-bar").getBoundingClientRect().height);
    await page.setViewportSize({ width: 420, height: 800 }); // bar now wraps to two rows
    await page.waitForTimeout(150);
    const bar2 = await page.evaluate(() => document.getElementById("sequence-bar").getBoundingClientRect().height);
    ok("narrow screens wrap the chips onto their own row (bar gets taller)", bar2 > bar1 + 20, `${bar1} -> ${bar2}`);
    const gap = await page.evaluate(() => {
      const bar = document.getElementById("sequence-bar").getBoundingClientRect();
      return ["#btn-logs-fab", "#btn-notifications-fab", "#bottom-status-bar"].map((s) => bar.top - document.querySelector(s).getBoundingClientRect().bottom);
    });
    ok("after the bar grows, Logs / Notifications / online pill still clear its top edge", gap.every((g) => g >= 4), JSON.stringify(gap));
    const chipX = await page.evaluate(() => window.__probe(".seq-chip__x"));
    ok("420px: the chip row has real width (the first chip's × is clickable)", chipX.centre && chipX.inViewport);
    await page.close();
  }

  // ---- 5. toasts don't land on top of Run Sequence ---------------------------
  {
    const page = await open(1500, 900);
    await page.evaluate(() => { const h = document.createElement("div"); h.id = "jui-toasts"; h.className = "jui-toasts"; const t = document.createElement("div"); t.className = "jui-toast is-in"; t.textContent = 'Added "x" to sequence.'; h.appendChild(t); document.body.appendChild(h); window.__setBar(2); });
    const r = await page.evaluate(() => ({ bar: document.getElementById("sequence-bar").getBoundingClientRect().top, toast: document.querySelector(".jui-toasts").getBoundingClientRect().bottom }));
    ok("toast stack sits above the bar, not over Run Sequence", r.toast <= r.bar, `toast.bottom=${r.toast} bar.top=${r.bar}`);
    await page.close();
  }

  await browser.close();
  server.close();
  console.log(`\n${passed} checks passed, ${failures.length} failed`);
  if (failures.length) { console.log("FAILURES:\n  " + failures.join("\n  ")); process.exit(1); }
})().catch((e) => { console.error(e); process.exit(2); });
