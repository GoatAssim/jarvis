// BUG-1 regression: a model reply with prose followed by raw HTML containing
// <style> used to survive DOMPurify's default config and restyle the whole
// Jarvis page. Runs the REAL MD_SANITIZE_CONFIG from web/public/app.js through
// real marked + real DOMPurify inside a real browser (Chromium via Playwright),
// since DOMPurify needs a genuine DOM. The test fails if the config is missing.
//
//   npm install marked@15 dompurify@3 playwright && npx playwright install chromium
//   node tests/verify_markdown_sanitize.js
// Override module locations with MARKED_JS / PURIFY_JS / PLAYWRIGHT env vars.

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "public", "app.js"), "utf8");
const m = src.match(/const MD_SANITIZE_CONFIG = (\{[\s\S]*?\n  \});/);
if (!m) { console.error("FAIL: MD_SANITIZE_CONFIG not found in app.js"); process.exit(1); }
const CONFIG_SRC = m[1];

const markedJs = process.env.MARKED_JS || require.resolve("marked/lib/marked.umd.js");
const purifyJs = process.env.PURIFY_JS || require.resolve("dompurify/dist/purify.min.js");
const { chromium } = require(process.env.PLAYWRIGHT || "playwright");

const CASES = [
  ["prose then raw <style>", "Here is the page:\n\n<style>body{background:red}*{margin:0}</style>\n<h1>Clock</h1>"],
  ["style inside a div", "Look:\n\n<div><style>.x{color:red}</style><p>hi</p></div>"],
  ["inline style attribute", "Look:\n\n<p style=\"position:fixed;inset:0;background:red\">cover</p>"],
  ["link stylesheet", "Look:\n\n<link rel=\"stylesheet\" href=\"https://evil.example/x.css\">\n<p>hi</p>"],
  ["meta refresh", "Look:\n\n<meta http-equiv=\"refresh\" content=\"0;url=https://evil.example\">\n<p>hi</p>"],
  ["base tag", "Look:\n\n<base href=\"https://evil.example/\">\n<p>hi</p>"],
];

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setContent("<!doctype html><html><body><div id='probe'>probe</div></body></html>");
  await page.addScriptTag({ path: markedJs });
  await page.addScriptTag({ path: purifyJs });
  const results = await page.evaluate(({ cases, cfgSrc }) => {
    const cfg = eval("(" + cfgSrc + ")");
    return cases.map(([name, md]) => {
      const html = marked.parse(md, { breaks: true, gfm: true });
      const out = {};
      for (const [label, clean] of [["default", DOMPurify.sanitize(html)], ["fixed", DOMPurify.sanitize(html, cfg)]]) {
        const host = document.createElement("div");
        host.innerHTML = clean;
        out[label] = !!host.querySelector("style,link,meta,base,[style]");
      }
      return { name, ...out };
    });
  }, { cases: CASES, cfgSrc: CONFIG_SRC });

  // Real-world check: injecting the fixed output must not restyle the page.
  const live = await page.evaluate(({ cfgSrc, md }) => {
    const cfg = eval("(" + cfgSrc + ")");
    const before = getComputedStyle(document.body).backgroundColor;
    const d = document.createElement("div");
    d.innerHTML = DOMPurify.sanitize(marked.parse(md, { breaks: true, gfm: true }), cfg);
    document.body.appendChild(d);
    return { before, after: getComputedStyle(document.body).backgroundColor, text: d.textContent };
  }, { cfgSrc: CONFIG_SRC, md: CASES[0][1] });
  await browser.close();

  let bad = 0;
  for (const r of results) {
    const ok = r.fixed === false;
    if (!ok) bad++;
    console.log((ok ? "PASS " : "FAIL ") + r.name + "  (default config leaks: " + r.default + ", fixed config leaks: " + r.fixed + ")");
  }
  if (!results.some((r) => r.default)) { console.log("FAIL: test is vacuous, default config leaked nothing"); bad++; }
  const restyled = live.before !== live.after;
  console.log((restyled ? "FAIL " : "PASS ") + "page background unchanged after injecting fixed output");
  if (restyled) bad++;
  if (!live.text.includes("Clock")) { console.log("FAIL: legitimate content was dropped"); bad++; }
  process.exit(bad ? 1 : 0);
})();
