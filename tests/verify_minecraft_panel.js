// verify_minecraft_panel.js -- the pure half of web/public/minecraft-panel.js (wave S3).
// Loads the panel file into a bare vm context (no DOM needed: only the helpers are used), then checks:
// the arguments it builds for the six tools, the one-line problems it reports, how it classifies an answer,
// that a link is made only for https on a vendor's own host, and the hand-written hand_s3_* fixtures.
// Run: node tests/verify_minecraft_panel.js        (not run by the agent that wrote it)
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const src = fs.readFileSync(path.join(ROOT, "web", "public", "minecraft-panel.js"), "utf8");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "minecraft-panel.js" });
const P = sandbox.JarvisMinecraft && sandbox.JarvisMinecraft._pure;

let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) pass++; else { fail++; console.log("FAIL: " + name + (extra !== undefined ? "  -> " + JSON.stringify(extra) : "")); }
}
const fx = (n) => JSON.parse(fs.readFileSync(path.join(ROOT, "tests", "data", "modhosts", n), "utf8"));

check("the panel exports its pure helpers", !!P && typeof P.searchArgs === "function");
check("it only ever names the six Minecraft tools", JSON.stringify(P.TOOLS.slice().sort()) === JSON.stringify(
  ["download_mc_file", "download_optifine", "get_mc_project", "list_optifine_versions", "make_modpack_file", "search_mc_mods"]), P.TOOLS);

// ---- arguments
let a = P.searchArgs({ query: "  sodium ", source: "", kind: "mod", mc_version: " 1.20.1", loader: "FABRIC", sort: "downloads", limit: 10, offset: 0 });
check("search: trimmed query, loader lower-cased, empty source left out (the tool's own default)", a.query === "sodium" && a.loader === "fabric" && !("source" in a) && a.mc_version === "1.20.1", a);
check("search: an unknown kind or sort falls back, never passes through", P.searchArgs({ query: "x", kind: "evil", sort: "nope" }).kind === "mod" && P.searchArgs({ query: "x", kind: "evil", sort: "nope" }).sort === "relevance");
check("search: limit stays in 1..10 and offset only when positive", !("limit" in P.searchArgs({ query: "x", limit: 99 })) && P.searchArgs({ query: "x", offset: 20 }).offset === 20 && !("offset" in P.searchArgs({ query: "x", offset: 0 })));
check("search: a bad loader is dropped, not sent", !("loader" in P.searchArgs({ query: "x", loader: "rift" })));

a = P.projectArgs({ project: "sodium", source: "curseforge", channel: "beta", files_limit: "0" });
check("project: files_limit 0 (= all) is kept", a.files_limit === 0 && a.source === "curseforge" && a.channel === "beta", a);
check("project: source defaults to modrinth", P.projectArgs({ project: "x", source: "weird" }).source === "modrinth");

a = P.downloadArgs({ project: "sodium", mc_version: "1.20.1", loader: "fabric", dest: " D:/Mods ", with_dependencies: true }, true);
check("download: dry_run only when asked, dest trimmed, dependencies opt-in", a.dry_run === true && a.dest === "D:/Mods" && a.with_dependencies === true, a);
a = P.downloadArgs({ project: "sodium" }, false);
check("download: a real call carries no dry_run and no dependencies by default", !("dry_run" in a) && !("with_dependencies" in a) && !("dest" in a), a);

a = P.optifineArgs({ mc_version: "1.20.1", preview: true }, false);
check("optifine: preview flag passes, no dry_run", a.preview === true && !("dry_run" in a), a);
check("optifine list: empty form is the overview", Object.keys(P.optifineListArgs({})).length === 0);

// ---- pack entries
const ents = P.parseEntries("sodium\n\n  lithium | abc123 \niris | | beta\r\nbad | | nonsense\n |\n");
check("pack entries: one per line, blank lines skipped, file id and channel parsed",
  ents.length === 4 && ents[0].project === "sodium" && ents[1].file === "abc123" && ents[2].channel === "beta" && !("channel" in ents[3]), ents);
check("pack entries are capped", P.parseEntries(Array.from({ length: 900 }, (_, i) => "p" + i).join("\n")).length === 500);
a = P.packArgs({ format: "curseforge", name: "My Pack", mc_version: "1.20.1", loader: "neoforge", entries: "sodium", include_dependencies: false, overwrite: true }, true);
check("pack: flags only when they differ from the tool's defaults", a.include_dependencies === false && a.overwrite === true && a.dry_run === true && a.loader === "neoforge", a);
a = P.packArgs({ name: "P", mc_version: "1.20.1", entries: "sodium", include_dependencies: true }, false);
check("pack: defaults (include_dependencies true) are not sent, loader_version omitted so the tool picks the latest",
  !("include_dependencies" in a) && !("loader_version" in a) && a.format === "modrinth" && a.loader === "fabric", a);
check("pack: vanilla is an accepted loader", P.packArgs({ loader: "vanilla", name: "P", mc_version: "1.20.1", entries: "x" }).loader === "vanilla");

// ---- plain-words problems before any call
check("problem: empty search", P.problem("search", { query: "" }) !== "");
check("problem: a pack needs a name, a version and a project", P.problem("pack", { name: "", mc_version: "1", entries: [{}] }) !== ""
  && P.problem("pack", { name: "x", mc_version: "", entries: [{}] }) !== "" && P.problem("pack", { name: "x", mc_version: "1.20.1", entries: [] }) !== ""
  && P.problem("pack", { name: "x", mc_version: "1.20.1", entries: [{ project: "a" }] }) === "");

// ---- classifying an answer
const nokey = fx("hand_s3_search_both_nokey.json");
check("a search with a missing CurseForge key still counts as ok", P.classify(nokey).kind === "ok");
check("...and the panel knows to point at Settings", P.wantsNoKeyHint(nokey) === true && P.wantsNoKeyHint(fx("hand_s3_project_files.json")) === false);
check("the hub's refused folder question is its own case", P.classify(fx("hand_s3_not_confirmed.json")).kind === "not_confirmed");
check("a dry run is ok", P.classify(fx("hand_s3_download_dry.json")).kind === "ok");
check("a timeout is recognised from the server's own words", P.classify({ ok: false, error: "timed out" }).kind === "timeout" && P.classify({ ok: false, reason: "timeout", error: "x" }).kind === "timeout");
check("no_key and ambiguous are their own cases", P.classify({ ok: false, reason: "no_key", error: "e" }).kind === "no_key" && P.classify({ ok: false, reason: "ambiguous", error: "e" }).kind === "ambiguous");
check("nothing at all is a failure, not a crash", P.classify(null).kind === "fail" && P.classify("x").kind === "fail");

// ---- the folder question
check("an omitted dest, ~, a drive and a rooted path count as outside the library; a sub-folder does not",
  P.isOutsideLibrary("") && P.isOutsideLibrary("~/Downloads") && P.isOutsideLibrary("D:\\Mods") && P.isOutsideLibrary("/home/x") && !P.isOutsideLibrary("Minecraft/mods") && !P.isOutsideLibrary(P.LIBRARY_FOLDER));

// ---- untrusted vendor text
const hostile = fx("hand_s3_hostile.json").hits[0];
check("a javascript: page address never becomes a link", P.safeUrl(hostile.page_url) === null);
check("only https on a vendor's own host is linkable", P.safeUrl("https://modrinth.com/mod/sodium") !== null && P.safeUrl("https://www.curseforge.com/x") !== null
  && P.safeUrl("https://optifine.net/downloads") !== null && P.safeUrl("http://modrinth.com/x") === null && P.safeUrl("https://evil.example/modrinth.com") === null
  && P.safeUrl("https://modrinth.com.evil.example/") === null && P.safeUrl("https://user:pw@modrinth.com/") === null && P.safeUrl("data:text/html,x") === null);
check("text() strips control and bidi-override characters but keeps the markup as plain characters",
  P.text("a\u202eb\u0000c") === "abc" && P.text(hostile.name) === hostile.name);
check("text() clips", P.text("x".repeat(50), 10).length === 10);
check("numbers and sizes read well", P.count(12345678) === "12.3M" && P.count(999) === "999" && P.size(1572864) === "1.5 MB" && P.size(null) === "" && P.day("2026-10-01T10:00:00Z") === "2026-10-01");

// ---- static: the panel file never builds HTML from text and never touches the key
const noComments = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc"]) {
  check("minecraft-panel.js never uses " + bad, noComments.indexOf(bad) < 0);
}
check("minecraft-panel.js never names the key file or a key reader", !/curseforge\.json|get_key|api[_-]?key|x-api-key/i.test(noComments));
check("minecraft-panel.js talks only to the tool routes", (noComments.match(/"\/api\/[^"]*"/g) || []).every((u) => u === '"/api/tools/preview"' || u === '"/api/tools/run"'), noComments.match(/"\/api\/[^"]*"/g));
check("minecraft-panel.js never calls a vendor directly", !/https?:\/\/(?!modrinth|www\.curseforge)/.test(noComments.replace(/"https?:\/\/[^"]*"/g, "")) && !/fetch\(\s*["']https?:/.test(noComments));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
