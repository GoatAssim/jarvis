// verify_minecraft_panel.js -- web/public/minecraft-panel.js without a browser (waves S3 and M2).
// Loads the panel file into a vm context (no DOM: only the pure helpers and the DOM-free write flows are used) and checks:
//   * the arguments it builds for the seven Minecraft tools, the one-line problems it reports, how it classifies an answer;
//   * that a link is made only for https on a vendor's own host, and that vendor text is never HTML;
//   * M2: the Updates helpers (arguments, the queue button's fields, not-checked words, the clarification inputs);
//   * M2: the cross-platform pack (direction, counts, what the dry run must expose before a yes is passed on);
//   * M2, the safety core: the ORDER and CONTENT of every write flow (dry run first, one dialog, then the real call),
//     when confirm_ui may be sent (only for a real write, only after the dialog, never on a dry run) and when
//     download_to_match may be sent (only after a dry run that gave the file count and the total size).
// What is NOT checked here (no DOM): how the tabs, tables and buttons are drawn and wired. That is checked by reading and by
// the manual checks in the delivery notes.
// Fixtures: tests/data/modhosts/hand_m2_*.json (the folder is git-ignored: `git add -f` them).
// Run: node tests/verify_minecraft_panel.js        (not run by the agent that wrote it)
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const src = fs.readFileSync(path.join(ROOT, "web", "public", "minecraft-panel.js"), "utf8");

// A bare vm context has no URL and no fetch: the panel's safeUrl needs URL, runTool needs fetch and JarvisUI.
function load(extra) {
  const sandbox = Object.assign({ URL }, extra || {});
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "minecraft-panel.js" });
  return sandbox.JarvisMinecraft;
}
const M = load();
const P = M && M._pure;
const F = M && M._flow;

let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) pass++; else { fail++; console.log("FAIL: " + name + (extra !== undefined ? "  -> " + JSON.stringify(extra) : "")); }
}
const fx = (n) => JSON.parse(fs.readFileSync(path.join(ROOT, "tests", "data", "modhosts", n), "utf8"));
const clone = (o) => JSON.parse(JSON.stringify(o));
const SEVEN = ["check_mc_updates", "download_mc_file", "download_optifine", "get_mc_project", "list_optifine_versions", "make_modpack_file", "search_mc_mods"];

check("the panel exports its pure helpers and flows", !!P && typeof P.searchArgs === "function" && !!F && typeof F.packWrite === "function");
check("it only ever names the seven Minecraft tools", JSON.stringify(P.TOOLS.slice().sort()) === JSON.stringify(SEVEN), P.TOOLS);
check("only the three writers can ever be sent a yes", JSON.stringify(P.WRITERS.slice().sort()) === JSON.stringify(["download_mc_file", "download_optifine", "make_modpack_file"]), P.WRITERS);

// ---- arguments (S3)
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

// ---- pack entries and arguments
const ents = P.parseEntries("sodium\n\n  lithium | abc123 \niris | | beta\r\nbad | | nonsense\n |\n");
check("pack entries: one per line, blank lines skipped, file id and channel parsed",
  ents.length === 4 && ents[0].project === "sodium" && ents[1].file === "abc123" && ents[2].channel === "beta" && !("channel" in ents[3]), ents);
check("pack entries are capped", P.parseEntries(Array.from({ length: 900 }, (_, i) => "p" + i).join("\n")).length === 500);
a = P.packArgs({ format: "curseforge", name: "My Pack", mc_version: "1.20.1", loader: "neoforge", entries: "sodium", include_dependencies: false, overwrite: true }, true);
check("pack: flags only when they differ from the tool's defaults", a.include_dependencies === false && a.overwrite === true && a.dry_run === true && a.loader === "neoforge", a);
a = P.packArgs({ name: "P", mc_version: "1.20.1", entries: "sodium", include_dependencies: true }, false);
check("pack: defaults (include_dependencies true) are not sent, loader_version omitted so the tool picks the latest",
  !("include_dependencies" in a) && !("loader_version" in a) && a.format === "modrinth" && a.loader === "fabric", a);
check("pack: an ordinary pack carries no entries_from and no download_to_match", !("entries_from" in a) && !("download_to_match" in a), a);
check("pack: vanilla is an accepted loader", P.packArgs({ loader: "vanilla", name: "P", mc_version: "1.20.1", entries: "x" }).loader === "vanilla");

// ---- M2: the cross-platform arguments
check("direction: the same platform, or none, is an ordinary pack", P.crossDirection("modrinth", "") === "" && P.crossDirection("modrinth", "modrinth") === "" && P.crossDirection("curseforge", "curseforge") === "" && P.crossDirection("curseforge", "weird") === "");
check("direction: CurseForge entries into a Modrinth pack, and the other way round",
  P.crossDirection("modrinth", "curseforge") === "curseforge_to_modrinth" && P.crossDirection("curseforge", "modrinth") === "modrinth_to_curseforge");
let f = { format: "modrinth", entries_from: "curseforge", name: "P", mc_version: "1.20.1", loader: "fabric", entries: "sodium", download_to_match: true };
a = P.packArgs(f, false);
check("pack: entries_from is sent when it differs from the pack's platform", a.entries_from === "curseforge", a);
check("pack: download_to_match is NOT sent for CurseForge -> Modrinth, which downloads nothing, even if the box is ticked", !("download_to_match" in a), a);
f = { format: "curseforge", entries_from: "modrinth", name: "P", mc_version: "1.20.1", loader: "fabric", entries: "sodium", download_to_match: true };
check("pack: download_to_match is sent only for Modrinth -> CurseForge, a real call, box ticked",
  P.packArgs(f, false).download_to_match === true && !("download_to_match" in P.packArgs(f, true)) && !("download_to_match" in P.packArgs(Object.assign({}, f, { download_to_match: false }), false)) && !("download_to_match" in P.packArgs(Object.assign({}, f, { download_to_match: "yes" }), false)));
check("pack: a dry run never carries download_to_match", P.packArgs(f, true).dry_run === true && !("download_to_match" in P.packArgs(f, true)));

// ---- M2: Updates arguments
a = P.updatesArgs({ folder: "  D:/Minecraft/mods ", mc_version: " 1.20.1", loader: "QUILT", channel: "beta" });
check("updates: folder trimmed, version, loader lower-cased, channel", a.folder === "D:/Minecraft/mods" && a.mc_version === "1.20.1" && a.loader === "quilt" && a.channel === "beta", a);
a = P.updatesArgs({ folder: "", mc_version: "", loader: "rift", channel: "weird" });
check("updates: an empty form sends only the channel default (the tool picks its own folder); a bad loader or channel is dropped", JSON.stringify(a) === JSON.stringify({ channel: "release" }), a);
check("updates: no field is required", P.problem("updates", a) === "");

const upd = fx("hand_m2_updates.json");
let q = P.updateDownloadFields(upd.updates[0], { mc_version: "1.20.1", loader: "fabric", channel: "release", dest: " D:/New " });
check("queue button: the fields come from get_with (project, file, source), with the form's version, loader and folder",
  q && q.project === "AANobbMI" && q.file === "u1Qe3xyz" && q.source === "modrinth" && q.mc_version === "1.20.1" && q.loader === "fabric" && q.dest === "D:/New", q);
check("queue button: those fields make the same download arguments the Project tab does, pinned to the file",
  (() => { const d = P.downloadArgs(q, true); return d.project === "AANobbMI" && d.file === "u1Qe3xyz" && d.dry_run === true && d.dest === "D:/New"; })());
check("queue button: an empty folder leaves dest out (the default folder)", !("dest" in P.updateDownloadFields(upd.updates[1], {})));
const hostileUpd = fx("hand_m2_updates_hostile.json");
check("queue button: an unknown source falls back to modrinth, never passes through", P.updateDownloadFields(hostileUpd.updates[0], {}).source === "modrinth");
check("queue button: no get_with, another tool, or a missing file id means nothing to queue",
  P.updateDownloadFields({ file: "x" }, {}) === null && P.updateDownloadFields({ get_with: { tool: "make_modpack_file", project: "a", file: "b" } }, {}) === null
  && P.updateDownloadFields({ get_with: { tool: "download_mc_file", project: "a" } }, {}) === null && P.updateDownloadFields(null, {}) === null);

// ---- M2: Updates words
check("why: a known code is put in words and the words say nothing was guessed for an unknown file", /does not know this file/.test(P.whyWords("not_on_modrinth", "")) && /guess/.test(P.whyWords("not_on_modrinth", "")));
check("why: the two questions are put as questions to answer", /Say which one you play/.test(P.whyWords("needs_mc_version", "")) && /Say which one you use/.test(P.whyWords("needs_loader", "")));
check("why: an unknown code shows the tool's own sentence, else the code", P.whyWords("brand_new_reason", "A reason the panel has no words for.") === "A reason the panel has no words for." && /brand_new_reason/.test(P.whyWords("brand_new_reason", "")));
check("why: no code at all is still a sentence", P.whyWords("", "") === "Not checked.");
check("folder_from in words", P.folderFromWords("given") === "the folder you named" && P.folderFromWords("last_checked") === "the last folder checked" && P.folderFromWords("default") === "your default download folder" && P.folderFromWords("somewhere new") === "somewhere new");
check("folder_from: Object.prototype names are not codes", P.folderFromWords("constructor") === "constructor" && P.whyWords("constructor", "") === "Not checked (constructor).");
const ask = fx("hand_m2_updates_ask.json");
let need = P.clarifyNeeds(ask);
check("clarification: both inputs when both questions are open", need.mc_version === true && need.loader === true, need);
need = P.clarifyNeeds(Object.assign(clone(ask), { not_checked: [{ file: "a.jar", why: "needs_loader" }] }));
check("clarification: only the loader when only that is open", need.mc_version === false && need.loader === true, need);
need = P.clarifyNeeds({ needs_clarification: true, ask: "something new", not_checked: [] });
check("clarification: an ask the panel cannot read still gets both inputs", need.mc_version === true && need.loader === true);
check("clarification: none when the tool asked nothing", P.clarifyNeeds(upd).mc_version === false && P.clarifyNeeds(upd).loader === false && P.clarifyNeeds(null).loader === false);
check("updates fixture: every existing key is still there and unknown ones are simply carried", !!upd.updates[0].new_optional_field && Array.isArray(upd.not_checked) && upd.folder_from === "given");

// ---- M2: the counts of a cross-platform result
let s = P.crossSummary(fx("hand_m2_pack_cross_dry.json"));
check("cross counts (dry run, CurseForge -> Modrinth): matched, skipped, ambiguous, not distributable, hash mismatch",
  s.present && s.matched === 2 && s.skipped === 4 && s.ambiguous === 1 && s.not_distributable === 1 && s.hash_mismatch === 1 && s.method === "sha1" && s.from === "curseforge" && s.to === "modrinth" && s.downloaded_files === 0, s);
s = P.crossSummary(fx("hand_m2_pack_cross_written.json"));
check("cross counts (written, Modrinth -> CurseForge): the files read are reported", s.present && s.matched === 2 && s.hash_mismatch === 1 && s.downloaded_files === 3 && s.downloaded_bytes === 5242880 && s.method === "fingerprint", s);
check("cross counts: an ordinary pack has no cross block", P.crossSummary({ ok: true, skipped: [{ reason: "not_found" }] }).present === false && P.crossSummary(null).present === false);
check("cross counts: a list the tool cut short still counts every skipped entry", P.crossSummary({ crossmatch: { matched: 1, entries: 9 }, skipped: [{ reason: "not_found" }], skipped_more: 7 }).skipped === 8);
check("skip words: known codes in words, unknown ones as they are", /identical/.test(P.skipWords("not_found")) && P.skipWords("brand_new") === "brand_new" && P.skipWords("") === "skipped");

// ---- M2: what the dry run must expose before downloads are allowed
let dn = P.downloadNeeds(fx("hand_m2_pack_cross_needs_download.json"));
check("downloads: the question's count, size and first names are read", dn.needed && dn.exposed && dn.count === 3 && dn.bytes === 5242880 && dn.files.length === 3, dn);
dn = P.downloadNeeds(fx("hand_m2_pack_cross_needs_download_nosize.json"));
check("downloads: a total of 0 or a file with no size is NOT exposed (no yes will be passed on)", dn.needed === true && dn.exposed === false, dn);
check("downloads: a count of 0, or a missing count, is not exposed", P.downloadNeeds({ needs_download: true, download_bytes: 10, download_files: [] }).exposed === false && P.downloadNeeds({ needs_download: true, download_count: 0, download_bytes: 10 }).exposed === false);
check("downloads: an ordinary dry run needs none", P.downloadNeeds(fx("hand_m2_pack_cross_dry.json")).needed === false && P.downloadNeeds(null).needed === false);

// ---- plain-words problems before any call
check("problem: empty search", P.problem("search", { query: "" }) !== "");
check("problem: a pack needs a name, a version and a project", P.problem("pack", { name: "", mc_version: "1", entries: [{}] }) !== ""
  && P.problem("pack", { name: "x", mc_version: "", entries: [{}] }) !== "" && P.problem("pack", { name: "x", mc_version: "1.20.1", entries: [] }) !== ""
  && P.problem("pack", { name: "x", mc_version: "1.20.1", entries: [{ project: "a" }] }) === "");

// ---- classifying an answer
const nokey = { ok: true, hits: [], curseforge_reason: "no_key", curseforge_error: "CurseForge needs a personal key." };
check("a search with a missing CurseForge key still counts as ok", P.classify(nokey).kind === "ok");
check("...and the panel knows to point at Settings", P.wantsNoKeyHint(nokey) === true && P.wantsNoKeyHint({ ok: true, files: [] }) === false);
check("the tool's own refused question is its own case", P.classify({ ok: false, reason: "bad_dest", error: "Not confirmed: nothing was queued.", cancelled: true }).kind === "not_confirmed");
check("the owner's own No in the panel's dialog is a different case (it must not blame the server)", P.classify({ ok: false, cancelled: true, reason: "cancelled", error: "Cancelled: nothing was run." }).kind === "cancelled");
check("a dry run is ok", P.classify({ ok: true, dry_run: true, message: "Dry run: nothing was queued." }).kind === "ok");
check("a timeout is recognised from the server's own words", P.classify({ ok: false, error: "timed out" }).kind === "timeout" && P.classify({ ok: false, reason: "timeout", error: "x" }).kind === "timeout");
check("no_key and ambiguous are their own cases", P.classify({ ok: false, reason: "no_key", error: "e" }).kind === "no_key" && P.classify({ ok: false, reason: "ambiguous", error: "e" }).kind === "ambiguous");
check("nothing at all is a failure, not a crash", P.classify(null).kind === "fail" && P.classify("x").kind === "fail");

// ---- the folder question
check("an omitted dest, ~, a drive and a rooted path count as outside the library; a sub-folder does not",
  P.isOutsideLibrary("") && P.isOutsideLibrary("~/Downloads") && P.isOutsideLibrary("D:\\Mods") && P.isOutsideLibrary("/home/x") && !P.isOutsideLibrary("Minecraft/mods") && !P.isOutsideLibrary(P.LIBRARY_FOLDER));

// ---- untrusted vendor text
check("a javascript: page address never becomes a link", P.safeUrl(hostileUpd.updates[0].project.page_url) === null && P.safeUrl("javascript:alert(1)") === null);
check("only https on a vendor's own host is linkable", P.safeUrl("https://modrinth.com/mod/sodium") !== null && P.safeUrl("https://www.curseforge.com/x") !== null
  && P.safeUrl("https://optifine.net/downloads") !== null && P.safeUrl("http://modrinth.com/x") === null && P.safeUrl("https://evil.example/modrinth.com") === null
  && P.safeUrl("https://modrinth.com.evil.example/") === null && P.safeUrl("https://user:pw@modrinth.com/") === null && P.safeUrl("data:text/html,x") === null);
check("text() strips control and bidi-override characters but keeps markup as plain characters",
  P.text("a\u202eb\u0000c") === "abc" && P.text(hostileUpd.updates[0].project.name) === hostileUpd.updates[0].project.name);
check("text() clips", P.text("x".repeat(50), 10).length === 10);
check("numbers and sizes read well", P.count(12345678) === "12.3M" && P.count(999) === "999" && P.size(1572864) === "1.5 MB" && P.size(null) === "" && P.day("2026-10-01T10:00:00Z") === "2026-10-01");

// ---- M2: the dialog's text and the exact arguments
const argLines = P.argsText({ format: "modrinth", name: "My Pack", dest: "D:\\Packs", entries: [{ project: "sodium" }, { project: "lithium", file: "abc" }], include_dependencies: false });
check("exact arguments: one per line, the folder in full, every pack project on its own line",
  /^format: modrinth$/m.test(argLines) && /^dest: D:\\Packs$/m.test(argLines) && /^entries \(2\):$/m.test(argLines) && /^  \{"project":"lithium","file":"abc"\}$/m.test(argLines) && /^include_dependencies: false$/m.test(argLines), argLines);
check("exact arguments: a very long list is cut with a visible ellipsis, never silently", (() => {
  const t = P.argsText({ entries: Array.from({ length: 900 }, (_, i) => ({ project: "mod-number-" + i })) });
  return t.length <= 6000 && t.endsWith("\u2026");
})());

check("mayPassYes: only a writer, only a real call, only after the dialog was answered yes in that call, only when asked",
  P.mayPassYes("download_mc_file", { project: "x" }, true, true) === true
  && P.mayPassYes("download_mc_file", { project: "x", dry_run: true }, true, true) === false
  && P.mayPassYes("download_mc_file", {}, false, true) === false && P.mayPassYes("download_mc_file", {}, true, false) === false
  && P.mayPassYes("download_mc_file", {}, true, "true") === false && P.mayPassYes("download_mc_file", {}, "yes", true) === false
  && P.mayPassYes("search_mc_mods", {}, true, true) === false && P.mayPassYes("check_mc_updates", {}, true, true) === false
  && P.mayPassYes("run_command", {}, true, true) === false && P.mayPassYes("make_modpack_file", {}, true, true) === true
  && P.mayPassYes("download_optifine", {}, true, true) === true);

const dryDl = { ok: true, dry_run: true, dest: "C:\\Users\\me\\Downloads", message: "Dry run: nothing was queued. It would queue sodium.jar in Downloads, to C:\\Users\\me\\Downloads." };
let plan = P.confirmPlan("download", dryDl, { project: "sodium" });
check("download dialog: the dry-run message and the folder", plan.ok && /Dry run: nothing was queued/.test(plan.summary) && /^Folder: C:\\Users\\me\\Downloads$/m.test(plan.summary), plan);
check("download dialog: no folder in the dry run means no yes is passed on, and it says what to do", (() => { const p = P.confirmPlan("download", { ok: true, message: "m" }, {}); return p.ok === false && /chat/.test(p.why); })());
plan = P.confirmPlan("download", Object.assign({}, dryDl, { dependencies_would_queue: [{ name_of_project: "Fabric API" }, { name_of_project: "Cloth" }] }), { project: "sodium", with_dependencies: true });
check("download dialog: dependencies that will also be queued are named, with their count", /2 required dependencies \(Fabric API, Cloth\)/.test(plan.summary), plan.summary);
plan = P.confirmPlan("optifine", { ok: true, dest: "D:\\Mods", message: "Dry run: would download OptiFine_1.20.1_HD_U_I6.jar" }, { mc_version: "1.20.1" });
check("OptiFine dialog: message and folder", plan.ok && /D:\\Mods/.test(plan.summary) && /OptiFine_1\.20\.1/.test(plan.summary));
plan = P.confirmPlan("pack", fx("hand_m2_pack_cross_dry.json"), { dest: "D:\\Packs", src: "D:\\Extra" });
check("pack dialog (CurseForge -> Modrinth): message, folder, file, extra folder, the match counts and 'nothing is downloaded'",
  plan.ok && /^Folder: D:\\Packs$/m.test(plan.summary) && /Cross Pack-1\.0\.0\.mrpack/.test(plan.summary) && /Extra files folder: D:\\Extra/.test(plan.summary) && /Matched on modrinth by sha1: 2 of 6 entries; 4 skipped\. Nothing is downloaded\./.test(plan.summary), plan.summary);
const dnOk = P.downloadNeeds(fx("hand_m2_pack_cross_needs_download.json"));
plan = P.confirmPlan("pack-download", fx("hand_m2_pack_cross_needs_download.json"), { dest: "D:\\Packs" }, dnOk);
check("pack dialog (Modrinth -> CurseForge): the file count, the total size, the host, the folder; the line written for the model is gone",
  plan.ok && /Downloads: 3 mod files, 5\.0 MB in all, from cdn\.modrinth\.com/.test(plan.summary) && /^Folder: D:\\Packs$/m.test(plan.summary) && !/Ask the owner/.test(plan.summary) && /Sodium, Lithium, Iris/.test(plan.summary), plan.summary);
plan = P.confirmPlan("pack-download", fx("hand_m2_pack_cross_needs_download.json"), { dest: "" }, dnOk);
check("pack dialog: a pack that must download needs the folder typed (the question that is asked does not name it)", plan.ok === false && /folder/.test(plan.why), plan);
plan = P.confirmPlan("pack-download", fx("hand_m2_pack_cross_needs_download_nosize.json"), { dest: "D:\\Packs" }, P.downloadNeeds(fx("hand_m2_pack_cross_needs_download_nosize.json")));
check("pack dialog: no size in the dry run means no yes is passed on, and it says to use chat", plan.ok === false && /chat/.test(plan.why), plan);

// ---- M2: the write flows, step by step (a recording stub stands in for runTool)
function stub(answers) {
  const log = [];
  const call = async (name, args, opts) => {
    log.push({ name, args: clone(args), opts: Object.assign({}, opts) });
    const next = answers.shift();
    return typeof next === "function" ? next(args) : next;
  };
  call.log = log;
  return call;
}
const queued = { ok: true, queued: true, message: "sodium.jar is queued in Downloads." };

(async () => {
  // download
  let call = stub([dryDl, queued]);
  let out = await F.downloadSteps({ project: "sodium", mc_version: "1.20.1", loader: "fabric" }, call);
  check("download flow: a dry run first, then one real call", call.log.length === 2 && call.log[0].args.dry_run === true && !("dry_run" in call.log[1].args) && out.stage === "done", call.log);
  check("download flow: the dry run carries no yes and no dialog", !call.log[0].opts.passYes && !call.log[0].opts.sure);
  check("download flow: the real call is sure, asks to pass the yes on (the folder is outside the library) and carries the dialog text",
    call.log[1].opts.sure === true && call.log[1].opts.passYes === true && /^Folder: C:\\Users\\me\\Downloads$/m.test(call.log[1].opts.summary) && /Dry run: nothing was queued/.test(call.log[1].opts.summary), call.log[1].opts);
  call = stub([dryDl, queued]);
  await F.downloadSteps({ project: "sodium", dest: "Minecraft/mods" }, call);
  check("download flow: a sub-folder of the library has no question, so no yes is asked for", call.log[1].opts.passYes === false, call.log[1].opts);
  call = stub([{ ok: true, message: "no folder named" }, queued]);
  out = await F.downloadSteps({ project: "sodium" }, call);
  check("download flow: a dry run that names no folder stops before the dialog", call.log.length === 1 && out.stage === "blocked" && /chat/.test(out.message), out);
  call = stub([{ ok: true, needs_clarification: true, message: "Which version?" }, queued]);
  out = await F.downloadSteps({ project: "sodium" }, call);
  check("download flow: a question from the dry run is shown, not confirmed", call.log.length === 1 && out.stage === "check");
  call = stub([{ ok: false, reason: "not_found", error: "no" }, queued]);
  out = await F.downloadSteps({ project: "sodium" }, call);
  check("download flow: a failed dry run stops there", call.log.length === 1 && out.stage === "check");
  call = stub([queued]);
  out = await F.downloadSteps({ project: "" }, call);
  check("download flow: a missing project is said before any call", call.log.length === 0 && out.stage === "invalid");
  call = stub([Object.assign({}, dryDl, { dependencies_would_queue: [{ name_of_project: "Fabric API" }] }), queued]);
  await F.downloadSteps({ project: "sodium", with_dependencies: true }, call);
  check("download flow: with dependencies the dialog names them and the real call asks for them", call.log[1].args.with_dependencies === true && /Fabric API/.test(call.log[1].opts.summary));

  // the Updates tab's queue button goes through the very same flow
  call = stub([dryDl, queued]);
  out = await F.downloadSteps(P.updateDownloadFields(upd.updates[0], {}), call);
  check("updates queue: dry run first, then the dialog, then the real call pinned to the file the tool named",
    call.log.length === 2 && call.log[0].args.dry_run === true && call.log[1].args.file === "u1Qe3xyz" && call.log[1].args.project === "AANobbMI" && call.log[1].opts.sure === true);

  // OptiFine
  const dryOf = { ok: true, dry_run: true, dest: "D:\\Mods", message: "Dry run: would download OptiFine_1.20.1_HD_U_I6.jar from optifine.net to D:\\Mods." };
  call = stub([dryOf, { ok: true, message: "Downloaded." }]);
  out = await F.optifineSteps({ mc_version: "1.20.1", dest: "D:\\Mods" }, call);
  check("OptiFine flow: dry run, one dialog with the folder, a real call that asks for the yes (always a full path)",
    call.log.length === 2 && call.log[0].args.dry_run === true && !("dry_run" in call.log[1].args) && call.log[1].opts.passYes === true && /D:\\Mods/.test(call.log[1].opts.summary) && out.stage === "done");
  call = stub([{ ok: true, message: "x" }]);
  out = await F.optifineSteps({ mc_version: "1.20.1" }, call);
  check("OptiFine flow: no folder in the dry run, no yes", call.log.length === 1 && out.stage === "blocked");

  // packs
  const base = { name: "Cross Pack", mc_version: "1.20.1", loader: "fabric", entries: "sodium\nlithium", dest: "D:\\Packs" };
  const dryPlain = { ok: true, dry_run: true, written: false, dest: "D:\\Packs", path: "D:\\Packs\\Cross Pack-1.0.0.mrpack", message: "Dry run: would write D:\\Packs\\Cross Pack-1.0.0.mrpack with 2 project files." };
  const wrote = fx("hand_m2_pack_cross_written.json");

  call = stub([dryPlain, wrote]);
  out = await F.packWrite(Object.assign({ format: "modrinth" }, base), call);
  check("pack flow (ordinary): dry run, then the real call with a yes for the folder question and nothing about matching",
    call.log.length === 2 && call.log[0].args.dry_run === true && !("dry_run" in call.log[1].args) && !("entries_from" in call.log[1].args) && !("download_to_match" in call.log[1].args)
    && call.log[1].opts.sure === true && call.log[1].opts.passYes === true && /^Folder: D:\\Packs$/m.test(call.log[1].opts.summary) && out.stage === "done", call.log);

  call = stub([fx("hand_m2_pack_cross_dry.json"), wrote]);
  out = await F.packWrite(Object.assign({ format: "modrinth", entries_from: "curseforge", download_to_match: true }, base), call);
  check("pack flow (CurseForge -> Modrinth): entries_from goes to both calls, download_to_match to neither, and the dialog says nothing is downloaded",
    call.log.length === 2 && call.log[0].args.entries_from === "curseforge" && call.log[1].args.entries_from === "curseforge"
    && !("download_to_match" in call.log[0].args) && !("download_to_match" in call.log[1].args) && /Nothing is downloaded\./.test(call.log[1].opts.summary) && call.log[1].opts.passYes === true, call.log);

  const needsDl = fx("hand_m2_pack_cross_needs_download.json");
  const cf = Object.assign({ format: "curseforge", entries_from: "modrinth" }, base);
  call = stub([needsDl, wrote]);
  out = await F.packWrite(Object.assign({ download_to_match: false }, cf), call);
  check("pack flow (Modrinth -> CurseForge, box NOT ticked): the count and size are shown, nothing is confirmed, nothing is downloaded or written",
    call.log.length === 1 && out.stage === "needs_tick" && out.need.exposed === true && /Tick/.test(out.message) && !("download_to_match" in call.log[0].args), out);

  call = stub([needsDl, wrote]);
  out = await F.packWrite(Object.assign({ download_to_match: true }, cf), call);
  check("pack flow (Modrinth -> CurseForge, ticked): the dry run does NOT carry download_to_match; the real call does, after one dialog with count, size and folder",
    call.log.length === 2 && call.log[0].args.dry_run === true && !("download_to_match" in call.log[0].args) && call.log[1].args.download_to_match === true && !("dry_run" in call.log[1].args)
    && call.log[1].opts.sure === true && call.log[1].opts.passYes === true
    && /Downloads: 3 mod files, 5\.0 MB in all/.test(call.log[1].opts.summary) && /^Folder: D:\\Packs$/m.test(call.log[1].opts.summary) && out.stage === "done", call.log);

  call = stub([needsDl, wrote]);
  out = await F.packWrite(Object.assign({ download_to_match: true }, cf, { dest: "" }), call);
  check("pack flow: a pack that must download and has no typed folder stops before the dialog", call.log.length === 1 && out.stage === "blocked" && /folder/.test(out.message), out);

  call = stub([fx("hand_m2_pack_cross_needs_download_nosize.json"), wrote]);
  out = await F.packWrite(Object.assign({ download_to_match: true }, cf), call);
  check("pack flow: a dry run without the total size means no dialog, no download_to_match, no yes", call.log.length === 1 && out.stage === "blocked" && /chat/.test(out.message) && !("download_to_match" in call.log[0].args), out);

  call = stub([Object.assign({}, dryPlain, { crossmatch: { from: "modrinth", to: "curseforge", method: "fingerprint", entries: 2, matched: 2, downloaded_files: 0, downloaded_bytes: 0, remembered: 2 } }), wrote]);
  out = await F.packWrite(Object.assign({ download_to_match: true }, cf), call);
  check("pack flow: when every fingerprint is already known there is nothing to download, so download_to_match is NOT sent even though the box is ticked",
    call.log.length === 2 && !("download_to_match" in call.log[1].args) && call.log[1].args.entries_from === "modrinth" && out.stage === "done", call.log);

  call = stub([{ ok: true, partial: true, written: false, message: "Stopped at the time limit." }, wrote]);
  out = await F.packWrite(Object.assign({ format: "modrinth" }, base), call);
  check("pack flow: a partial dry run is shown and nothing is written", call.log.length === 1 && out.stage === "check");

  call = stub([{ ok: false, reason: "no_compatible_file", error: "Nothing could be matched", skipped: [] }, wrote]);
  out = await F.packWrite(Object.assign({ format: "modrinth", entries_from: "curseforge" }, base), call);
  check("pack flow: a failed dry run is shown and nothing is written", call.log.length === 1 && out.stage === "check");

  call = stub([wrote]);
  out = await F.packWrite({ format: "modrinth", name: "", mc_version: "1.20.1", entries: "a" }, call);
  check("pack flow: a missing name is said before any call", call.log.length === 0 && out.stage === "invalid");

  call = stub([dryPlain]);
  out = await F.packCheck(Object.assign({ format: "curseforge", entries_from: "modrinth", download_to_match: true }, base), call);
  check("pack check only (dry run button): one dry call, never download_to_match, never a dialog of its own", call.log.length === 1 && call.log[0].args.dry_run === true && !("download_to_match" in call.log[0].args) && !call.log[0].opts.sure && !call.log[0].opts.passYes && out.stage === "checked");

  // runTool against a stub server and a stub dialog
  async function runWith(answerYes, name, args, opts, preview) {
    const seen = { fetches: [], dialogs: [] };
    const env = load({
      JarvisUI: { confirm: async (o) => { seen.dialogs.push(o); return answerYes; } },
      fetch: async (url, init) => {
        const body = JSON.parse(init.body);
        seen.fetches.push({ url, body });
        const data = url === "/api/tools/preview" ? (preview || { confirm_required: true }) : { ok: true, result: { ok: true, message: "ran" } };
        return { ok: true, json: async () => data };
      },
    });
    seen.result = await env._flow.runTool(name, args, opts);
    return seen;
  }
  let r = await runWith(true, "download_mc_file", { project: "sodium" }, { sure: true, passYes: true, title: "Queue?", summary: "SUMMARY LINE\nFolder: X" });
  let run = r.fetches.find((x) => x.url === "/api/tools/run");
  check("runTool: preview first, then ONE dialog with the summary and the exact arguments, then the run with confirm_ui",
    r.fetches[0].url === "/api/tools/preview" && r.dialogs.length === 1 && /SUMMARY LINE/.test(r.dialogs[0].body) && /Exact arguments:\nproject: sodium/.test(r.dialogs[0].body) && run && run.body.confirm_ui === true, r);
  r = await runWith(false, "download_mc_file", { project: "sodium" }, { sure: true, passYes: true });
  check("runTool: a No in the dialog runs nothing and reports a cancel, not a server problem", !r.fetches.some((x) => x.url === "/api/tools/run") && r.result.cancelled === true && r.result.reason === "cancelled" && P.classify(r.result).kind === "cancelled", r.result);
  r = await runWith(true, "download_mc_file", { project: "sodium", dry_run: true }, { sure: true, passYes: true });
  run = r.fetches.find((x) => x.url === "/api/tools/run");
  check("runTool: a dry run never carries confirm_ui, even if asked", run && !("confirm_ui" in run.body), run);
  r = await runWith(true, "download_mc_file", { project: "sodium" }, { sure: true });
  run = r.fetches.find((x) => x.url === "/api/tools/run");
  check("runTool: no confirm_ui unless the caller asks for it", run && !("confirm_ui" in run.body), run);
  r = await runWith(true, "search_mc_mods", { query: "x" }, { passYes: true }, { confirm_required: false });
  run = r.fetches.find((x) => x.url === "/api/tools/run");
  check("runTool: a lookup that needs no dialog runs without one and never carries confirm_ui", r.dialogs.length === 0 && run && !("confirm_ui" in run.body), r);
  r = await runWith(true, "check_mc_updates", { channel: "release" }, { passYes: true }, { confirm_required: false });
  run = r.fetches.find((x) => x.url === "/api/tools/run");
  check("runTool: the read-only update check never carries confirm_ui", run && !("confirm_ui" in run.body), run);
  r = await runWith(true, "make_modpack_file", { name: "p" }, { passYes: true }, { confirm_required: false });
  run = r.fetches.find((x) => x.url === "/api/tools/run");
  check("runTool: a writer whose dialog was never shown never carries confirm_ui", r.dialogs.length === 0 && run && !("confirm_ui" in run.body), r);
  r = await runWith(true, "run_command", { x: 1 }, { sure: true, passYes: true });
  check("runTool: a tool that is not a Minecraft tool is refused without any request", r.fetches.length === 0 && r.result.reason === "unsupported");
  r = await runWith(true, "check_mc_updates", { channel: "release" }, {}, { confirm_required: true });
  check("runTool: the update check obeys the server's confirmation setting like every other call", r.dialogs.length === 1 && r.dialogs[0].body.indexOf("Exact arguments:") >= 0);

  // ---- static: the panel file never builds HTML from text and never touches the key
  const noComments = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc"]) {
    check("minecraft-panel.js never uses " + bad, noComments.indexOf(bad) < 0);
  }
  check("minecraft-panel.js never names the key file or a key reader", !/curseforge\.json|get_key|api[_-]?key|x-api-key/i.test(noComments));
  check("minecraft-panel.js talks only to the tool routes", (noComments.match(/"\/api\/[^"]*"/g) || []).every((u) => u === '"/api/tools/preview"' || u === '"/api/tools/run"'), noComments.match(/"\/api\/[^"]*"/g));
  check("minecraft-panel.js never calls a vendor directly", !/https?:\/\/(?!modrinth|www\.curseforge)/.test(noComments.replace(/"https?:\/\/[^"]*"/g, "")) && !/fetch\(\s*["']https?:/.test(noComments));
  check("confirm_ui is written once, behind mayPassYes", (noComments.match(/confirm_ui/g) || []).length === 1 && /if \(mayPassYes\(name, args, shown, o\.passYes\)\) body\.confirm_ui = true;/.test(noComments));
  check("passYes is asked for only by the write flows (never by a lookup, an update check or a dry run)",
    (noComments.match(/passYes:/g) || []).length === 4 && (noComments.match(/sure: true/g) || []).length === 4);
  const strings = noComments.match(/"(?:[^"\\\n]|\\.)*"/g) || [];
  check("no word 'install' in any text the panel shows (nothing is ever installed)", !strings.some((t) => /install/i.test(t)), strings.filter((t) => /install/i.test(t)));
  check("no download_to_match is ever hard-coded to true outside the flow that checked the dry run", (noComments.match(/download_to_match: true/g) || []).length === 1);

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.log("FAIL: the test script itself threw: " + (e && e.stack || e)); process.exit(1); });
