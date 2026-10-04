// Downloads the pinned highlight.js build into web/public/vendor/ so the Ask
// panel's code blocks still get syntax colouring when the CDN is unreachable
// (master plan D-I1 / K.6.2: CDN primary, vendored copy as the fallback).
//
//   node web/scripts/vendor-highlightjs.mjs
//
// The version is read from the CDN <script> tag in web/public/index.html, so
// there is one place to bump: edit that tag, rerun this, commit both.
// Needs Node 18+ (global fetch) and network access; nothing else.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const pub = path.join(here, "..", "public");

const html = await readFile(path.join(pub, "index.html"), "utf8");
const m = /@highlightjs\/cdn-assets@(\d+\.\d+\.\d+)\/highlight\.min\.js/.exec(html);
if (!m) {
  console.error("Could not find the pinned @highlightjs/cdn-assets@X.Y.Z tag in web/public/index.html.");
  process.exit(1);
}
const version = m[1];
const url = `https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@${version}/highlight.min.js`;

const res = await fetch(url);
if (!res.ok) {
  console.error(`GET ${url} -> HTTP ${res.status}`);
  process.exit(1);
}
const body = await res.text();
if (!body.includes("hljs") || body.length < 20000) {
  console.error(`Unexpected response from ${url} (${body.length} bytes) - not writing it.`);
  process.exit(1);
}

const dir = path.join(pub, "vendor");
await mkdir(dir, { recursive: true });
await writeFile(path.join(dir, "highlight.min.js"), body, "utf8");
console.log(`Wrote web/public/vendor/highlight.min.js  (highlight.js ${version}, ${body.length} bytes)`);
