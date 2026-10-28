// Menu > Downloads add form (jarvis/actions/hub_ui/tool.js), N4: the form asks dl_add's dry run which
// engine takes each link, then calls that engine's own add tool.  The pure half, plus wiring checks.
//
// Covers: links grouped by the add tool the router named (order of first appearance; unknown or
// missing answers stay with dl_add, which routes again itself); one call per group, except the
// Stremio tool which takes one magnet at a time; the dest / group / start_paused / extras reaching
// every call; several answers merged into one (counts add up, a failure does not hide a success,
// a declined confirmation is kept); and, in the source, that the form no longer calls dl_add with
// the real links before asking, and only names tools that exist in this file.
//
// 9C (the reworked add menu) appends a second half: what each link is and which engine takes it, the dry run in chunks of ten, the
// option groups, yt-dlp's own choices (and that they reach dl_add_ytdlp only), what an add came to (added / partial / refused /
// declined / paused / scheduled / a question asked back), and checks on the markup and style of the form.
//
// Run: node tests/verify_hub_add_form.js
"use strict";
const fs = require("fs");
const path = require("path");

const ui = path.join(__dirname, "..", "jarvis-cli", "jarvis", "actions", "hub_ui");
const source = fs.readFileSync(path.join(ui, "tool.js"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () { /* probe */ }).constructor;

let passed = 0;
let failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log("ok      " + name); } else { failed++; console.log("FAILED  " + name); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

(async () => {
  const pure = await new AsyncFunction("host", source)({ __hubPureOnly: true });
  const dec = (t) => ({ ok: true, engine: "x", add_tool: t });

  // ---- grouping
  const urls = ["https://a/x.zip", "https://youtu.be/1", "magnet:?xt=urn:btih:" + "0".repeat(40), "https://youtu.be/2"];
  const g = pure.groupByAddTool(urls, [dec("dl_add"), dec("dl_add_ytdlp"), dec("dl_add_stremio"), dec("dl_add_ytdlp")]);
  check("links are grouped by the add tool the router named, in order of first appearance",
    eq(g, [{ tool: "dl_add", urls: [urls[0]] }, { tool: "dl_add_ytdlp", urls: [urls[1], urls[3]] }, { tool: "dl_add_stremio", urls: [urls[2]] }]));
  check("no answer at all: everything stays with dl_add", eq(pure.groupByAddTool(urls, undefined), [{ tool: "dl_add", urls: urls }]));
  check("a decision with no add_tool, or a junk one, stays with dl_add",
    eq(pure.groupByAddTool(urls.slice(0, 2), [{ ok: false }, { add_tool: "rm -rf" }]), [{ tool: "dl_add", urls: urls.slice(0, 2) }]));
  check("links past the dry run's ten decisions stay with dl_add",
    eq(pure.groupByAddTool(["a", "b"], [dec("dl_add_ytdlp")]), [{ tool: "dl_add_ytdlp", urls: ["a"] }, { tool: "dl_add", urls: ["b"] }]));
  check("a provider's generated tool name is accepted", pure.groupByAddTool(["a"], [dec("dl_add_model_fetch")])[0].tool === "dl_add_model_fetch");
  check("nothing to group", eq(pure.groupByAddTool([], []), []) && eq(pure.groupByAddTool(null, null), []));

  // ---- the calls
  const base = { dest: "Manuals", group: "Mine", start_paused: true, notify: "off" };
  const calls = pure.addCalls(g, base);
  check("one call per group, except Stremio which gets one per magnet", calls.length === 3
    && calls[0].tool === "dl_add" && eq(calls[0].args.urls, [urls[0]])
    && calls[1].tool === "dl_add_ytdlp" && eq(calls[1].args.urls, [urls[1], urls[3]])
    && calls[2].tool === "dl_add_stremio" && calls[2].args.magnet === urls[2] && calls[2].args.urls === undefined);
  check("dest, group, start_paused and the extras reach every call", calls.every((c) => c.args.dest === "Manuals" && c.args.group === "Mine"
    && c.args.start_paused === true && c.args.notify === "off"));
  check("the shared base is not mutated", eq(base, { dest: "Manuals", group: "Mine", start_paused: true, notify: "off" }));
  const two = pure.addCalls([{ tool: "dl_add_stremio", urls: ["m1", "m2"] }], {});
  check("two magnets make two Stremio calls", two.length === 2 && two[0].args.magnet === "m1" && two[1].args.magnet === "m2");
  check("all plain links make exactly the call the form always made",
    eq(pure.addCalls(pure.groupByAddTool(["a", "b"], []), { dest: "D" }), [{ tool: "dl_add", args: { dest: "D", urls: ["a", "b"] } }]));

  // ---- merging answers
  const one = { ok: true, count: 1, added: [{ id: "h_1" }] };
  check("one answer is passed through untouched", pure.mergeAddResults([one]) === one);
  const m = pure.mergeAddResults([one, { ok: true, count: 2, added: [{ id: "h_2" }, { id: "h_3" }], note: "daemon off", skipped: [{ problem: "p" }] }]);
  check("counts add up and the rows are kept", m.ok && m.count === 3 && m.added.length === 3 && m.skipped.length === 1 && m.note === "daemon off");
  const mixed = pure.mergeAddResults([{ ok: false, error: "yt-dlp isn't installed" }, one]);
  check("a failure does not hide a success (and its error is not shown)", mixed.ok === true && mixed.count === 1 && mixed.error === undefined);
  const bad = pure.mergeAddResults([{ ok: false, error: "first" }, { ok: false, error: "second" }]);
  check("all failed: ok is false and the first error is kept", bad.ok === false && bad.error === "first");
  check("a declined confirmation is kept", pure.mergeAddResults([one, { ok: false, declined: true }]).declined === true);
  check("nothing in, nothing queued", pure.mergeAddResults([]).ok === false && pure.mergeAddResults(null).count === 0);

  // ---- wiring, on the source
  const submit = source.slice(source.indexOf("async function submitAdd"), source.indexOf("// ---- the add form's extras"));
  // 9C: the dry run moved into dryRunAll (ten links per call), the arguments into readAddForm; these three cases follow them.
  const dryAll = source.slice(source.indexOf("async function dryRunAll"), source.indexOf("async function previewHint"));
  const readForm = source.slice(source.indexOf("function readAddForm"), source.indexOf("function resetAddOneOffs"));
  check("submitAdd asks the router with a dry run first", /callTool\("dl_add", \{ urls: part, dry_run: true \}\)/.test(dryAll) && /await dryRunAll\(form\.urls\)/.test(submit));
  check("submitAdd then calls the tool of each group", submit.includes("groupByAddTool(form.urls, checked.decisions)") && /callTool\(c\.tool, c\.args\)/.test(submit));
  check("submitAdd no longer sends the links in its own argument object", /const args = \{\};/.test(readForm) && !/args\.urls/.test(readForm));
  const named = new Set([...source.matchAll(/callTool\("([a-z_]+)"/g)].map((x) => x[1]));
  check("the literal tool names the panel calls are all in this file's tool list", [...named].every((n) => /^(dl_|hub_)/.test(n)));
  const py = fs.readFileSync(path.join(ui, "..", "download_hub.py"), "utf8");
  check("every add tool the panel can pick is a tool this file defines",
    ["dl_add", "dl_add_ytdlp", "dl_add_stremio"].every((n) => py.includes('"' + n + '": tool_' + n)));

  // ======================================================================== 9C: the reworked "Add a download" menu
  const sortKeys = (o) => (o && typeof o === "object" && !Array.isArray(o)) ? Object.keys(o).sort().reduce((a, k) => { a[k] = sortKeys(o[k]); return a; }, {}) : o;
  const same = (a, b) => JSON.stringify(sortKeys(a)) === JSON.stringify(sortKeys(b));
  const MAGNET = "magnet:?xt=urn:btih:" + "0".repeat(40);

  // ---- what each link is and who takes it
  check("kinds: a magnet, a hash, a .torrent link, a video page, a direct file, a torrent, anything else",
    pure.linkKindOf(MAGNET, null).id === "magnet" && pure.linkKindOf("a".repeat(40), null).id === "hash"
    && pure.linkKindOf("https://x.example/y.torrent?k=1", { engine: "stremio" }).id === "torrentfile"
    && pure.linkKindOf("https://youtu.be/1", { engine: "ytdlp" }).id === "video" && pure.linkKindOf("https://a.example/x.zip", { engine: "http" }).id === "file"
    && pure.linkKindOf("https://a.example/x", { engine: "qbittorrent" }).id === "torrent" && pure.linkKindOf("https://a.example/x", null).id === "link");
  check("engine words: yt-dlp, Stremio, HTTP, qBittorrent; an unknown engine id as given; none for nothing",
    pure.linkEngineWord("ytdlp") === "yt-dlp" && pure.linkEngineWord("stremio") === "Stremio" && pure.linkEngineWord("http") === "HTTP"
    && pure.linkEngineWord("qbittorrent") === "qBittorrent" && pure.linkEngineWord("model_fetch") === "model_fetch" && pure.linkEngineWord("") === "");
  check("torrents are counted by their shape", pure.torrentLinkCount([MAGNET, "https://a.example/x.zip", "b".repeat(40), "https://a.example/b.torrent"]) === 3 && pure.torrentLinkCount(null) === 0);

  const three = [{ ok: true, engine: "ytdlp", reason: "video page", add_tool: "dl_add_ytdlp" }, { ok: true, engine: "ytdlp", reason: "video page", add_tool: "dl_add_ytdlp" },
    { ok: true, engine: "stremio", reason: "magnet", add_tool: "dl_add_stremio" }];
  const s3 = pure.describeLinks(["https://youtu.be/1", "https://youtu.be/2", MAGNET], three);
  check("the one sentence: '3 links: 2 video pages (yt-dlp), 1 magnet (Stremio)'", s3.text === "3 links: 2 video pages (yt-dlp), 1 magnet (Stremio)");
  check("the same sentence for a screen reader says who takes each", s3.spoken === "3 links: 2 video pages taken by yt-dlp, 1 magnet taken by Stremio");
  check("it knows a video page and a torrent are in the list", s3.video === true && s3.torrents === 1 && s3.bad === 0 && s3.unchecked === 0 && s3.ok === 3);
  const mixedSum = pure.describeLinks(["https://a.example/x.zip", "https://a.example/bad.zip"], [{ ok: true, engine: "http", reason: "direct link (HTTP engine)", add_tool: "dl_add" }, { ok: false, problem: "private address" }]);
  check("a problem is named in the sentence and per link", mixedSum.text === "2 links: 1 direct file (HTTP) \u00B7 1 can't be added" && mixedSum.bad === 1
    && mixedSum.items[1].ok === false && mixedSum.items[1].checked === true && mixedSum.items[1].problem === "private address" && mixedSum.video === false);
  check("one link: no leading count on its kind, and the router's reason follows",
    pure.describeLinks(["https://a.example/x.zip"], [{ ok: true, engine: "http", reason: "direct link (HTTP engine)" }]).text === "1 link: direct file (HTTP) \u2014 direct link");
  check("the hub's reason loses its engine tail, and vanishes when it only repeats the kind", pure.reasonWords("YouTube page (yt-dlp engine)", null) === "YouTube page" && pure.reasonWords("direct link (HTTP engine)", null) === "direct link"
    && pure.reasonWords(".torrent file on this PC (Stremio engine)", null) === ".torrent file on this PC" && pure.reasonWords("magnet link (qBittorrent, preferred in X)", null) === "magnet link (qBittorrent, preferred in X)"
    && pure.reasonWords("video page (yt-dlp engine)", { one: "video page" }) === "" && pure.reasonWords(null, null) === "");
  check("every link refused: it says so", pure.describeLinks(["https://x.example"], [{ ok: false, problem: "nope" }]).text === "1 link: I can't add it"
    && pure.describeLinks(["https://x.example/1", "https://x.example/2"], [{ ok: false, problem: "a" }, { ok: false, problem: "b" }]).text === "2 links: none can be added");
  const partSum = pure.describeLinks(["https://a.example/1", "https://a.example/2"], [{ ok: true, engine: "http" }, null]);
  check("a link the hub did not answer for is 'not checked yet', never guessed", partSum.unchecked === 1 && partSum.items[1].checked === false && partSum.text === "2 links: 1 direct file (HTTP) \u00B7 1 not checked yet");
  check("a junk add_tool name is not trusted; nothing in, nothing out", pure.describeLinks(["https://a.example/1"], [{ ok: true, engine: "ytdlp", add_tool: "rm -rf" }]).video === false
    && pure.describeLinks([], []).text === "" && pure.describeLinks(null, null).items.length === 0);

  // ---- the dry run in chunks of ten
  check("chunked: 25 links make 10, 10, 5; nothing makes nothing", eq(pure.chunked(Array.from({ length: 25 }, (_, i) => i), 10).map((c) => c.length), [10, 10, 5]) && eq(pure.chunked([], 10), []) && eq(pure.chunked(null, 3), []));
  check("the chunk size is the hub's ten", pure.DRY_RUN_CHUNK === 10);
  const md = pure.mergeDecisions([["a", "b"], ["c"]], [{ decisions: [{ ok: true }, { ok: false }] }, { ok: false, error: "hub down" }]);
  check("mergeDecisions keeps the order, leaves null for a chunk that was not answered and keeps its error", md.decisions.length === 3 && md.decisions[0].ok === true && md.decisions[1].ok === false && md.decisions[2] === null && md.error === "hub down");
  check("mergeDecisions with no answers at all", eq(pure.mergeDecisions([["a"]], undefined), { decisions: [null], error: "" }));

  // ---- what the box will not use, a folder that is a full path, the button, the group states
  check("text that is not a link is named; links past 50 are named", pure.ignoredNote("https://a.example/x hello world") === "2 pieces of text that aren't links are ignored"
    && pure.ignoredNote("https://a.example/x hello") === "1 piece of text that isn't a link is ignored" && pure.ignoredNote("hello world") === "" && pure.ignoredNote("") === ""
    && pure.ignoredNote(Array.from({ length: 51 }, (_, i) => "https://a.example/" + i).join(" ")) === "only the first 50 links are used");
  check("a full path is told from a library sub-folder", ["D:\\Games", "D:/Games", "/home/me/x", "\\\\server\\share", "~", "~/x", " C:\\x "].every(pure.looksAbsolute)
    && !["Games/Old", "Manuals", "", null].some(pure.looksAbsolute));
  check("the button says what it will do", pure.addButtonLabel({ count: 0 }) === "Add to downloads" && pure.addButtonLabel({ count: 1 }) === "Add 1 download" && pure.addButtonLabel({ count: 3 }) === "Add 3 downloads"
    && pure.addButtonLabel({ count: 3, paused: true }) === "Add 3 downloads, paused" && pure.addButtonLabel({ count: 2, start: "tomorrow at 3am" }) === "Schedule 2 downloads"
    && pure.addButtonLabel({ count: 2, start: " Now " }) === "Add 2 downloads" && pure.addButtonLabel({ count: 1, start: "in 2 hours", paused: true }) === "Schedule 1 download");
  check("groups with nothing set say nothing", eq(pure.groupStates({}), { where: "", when: "", speed: "", tell: "", batch: "" }) && eq(pure.groupStates(undefined), { where: "", when: "", speed: "", tell: "", batch: "" }));
  const gs = pure.groupStates({ dest: "Games/Old", open: "reveal", start: "tomorrow at 3am", paused: true, limit: "2", unit: "mb", notify: "on", channels: ["discord"], events: ["done", "failed"], group: "  Mine " });
  check("groups with choices say them", gs.where === "in Games/Old \u00B7 then show the file in its folder" && gs.when === "starts tomorrow at 3am" && gs.speed === "limit 2 MB/s"
    && gs.tell === "always \u00B7 through discord \u00B7 when it finishes / fails" && gs.batch === "Mine");
  check("a full-path folder says it asks first; paused alone is named; a bad speed is flagged; 'now' is not a start time",
    pure.groupStates({ dest: "D:\\Games" }).where === "D:\\Games (outside the library: asks first)" && pure.groupStates({ paused: true }).when === "added paused"
    && pure.groupStates({ limit: "x", unit: "kb" }).speed === "check the speed" && pure.groupStates({ start: "now" }).when === "" && pure.groupStates({ notify: "off" }).tell === "never");

  // ---- yt-dlp's own choices: only what differs from the default, only what fits the mode, only tool arguments that exist
  check("no choices, no arguments", same(pure.videoOptionsFromForm({}).args, {}) && same(pure.videoOptionsFromForm(undefined).args, {}));
  check("video: quality, file type and codec when they differ from the default", same(pure.videoOptionsFromForm({ mode: "video", quality: "720p", container: "mkv", video_codec: "av1" }).args, { quality: "720p", container: "mkv", video_codec: "av1" })
    && same(pure.videoOptionsFromForm({ mode: "video", quality: "best", container: "mp4", video_codec: "" }).args, {}) && same(pure.videoOptionsFromForm({ quality: "999p", container: "avi", video_codec: "x" }).args, {}));
  check("audio: mode, a non-default file type and the quality; the video choices are not sent", same(pure.videoOptionsFromForm({ mode: "audio", audio_format: "flac", audio_quality: "192K", quality: "720p", container: "mkv", video_codec: "av1" }).args,
    { mode: "audio", audio_format: "flac", audio_quality: "192K" }) && same(pure.videoOptionsFromForm({ mode: "audio", audio_format: "mp3" }).args, { mode: "audio" }));
  check("ticked boxes are sent as true, unticked ones not at all; languages only with a subtitle box; SponsorBlock as typed",
    same(pure.videoOptionsFromForm({ embed_thumbnail: true, write_subs: true, sub_langs: "en,es", skip_if_downloaded: true, embed_subs: false }).args, { embed_thumbnail: true, write_subs: true, sub_langs: "en,es", skip_if_downloaded: true })
    && same(pure.videoOptionsFromForm({ sub_langs: "en" }).args, {}) && same(pure.videoOptionsFromForm({ sponsorblock_remove: " sponsor,selfpromo " }).args, { sponsorblock_remove: "sponsor,selfpromo" }));
  const py2 = fs.readFileSync(path.join(ui, "..", "download_hub.py"), "utf8");
  const videoArgs = [...(/_VIDEO_ARGS = \(([^)]*)\)/.exec(py2) || [0, ""])[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
  const everything = pure.videoOptionsFromForm({ mode: "video", quality: "1080p", container: "webm", video_codec: "vp9", embed_thumbnail: true, embed_metadata: true, embed_chapters: true, write_subs: true, embed_subs: true,
    skip_if_downloaded: true, sub_langs: "en", sponsorblock_remove: "sponsor" }).args;
  const everythingAudio = pure.videoOptionsFromForm({ mode: "audio", audio_format: "opus", audio_quality: "5", embed_thumbnail: true }).args;
  check("every argument the form can send is one of dl_add_ytdlp's own (read from download_hub.py, nothing invented)", videoArgs.length === 14
    && Object.keys(everything).concat(Object.keys(everythingAudio)).every((k) => videoArgs.indexOf(k) !== -1));
  const enumOf = (key) => [...((new RegExp('"' + key + '": \\{"type": "string", "enum": \\[([^\\]]*)\\]').exec(py2) || [0, ""])[1]).matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  check("the file types, codecs and audio types offered are the schema's own enums", eq(enumOf("container"), pure.VIDEO_CONTAINERS) && eq(enumOf("video_codec"), pure.VIDEO_CODECS) && eq(enumOf("audio_format"), pure.AUDIO_FORMATS));
  const qDesc = /"quality": \{"type": "string", "description": "([^"]*)"/.exec(py2);
  check("the qualities offered are the ones the schema names", !!qDesc && pure.VIDEO_QUALITIES.every((q) => qDesc[1].indexOf(q) !== -1));
  const coreNames = (/_CORE_NAMES = \(([^)]*)\)/.exec(py2) || [0, ""])[1];
  check("dl_add's own argument list holds none of the video arguments (N4)", coreNames.length > 10 && videoArgs.every((k) => coreNames.indexOf('"' + k + '"') === -1));

  // ---- the video choices reach dl_add_ytdlp and no other tool
  const gAll = [{ tool: "dl_add", urls: ["https://a.example/x.zip"] }, { tool: "dl_add_ytdlp", urls: ["https://youtu.be/1"] }, { tool: "dl_add_stremio", urls: [MAGNET] }];
  const perBase = { dest: "D" };
  const perCalls = pure.addCalls(gAll, perBase, { dl_add_ytdlp: { quality: "720p", mode: "video" } });
  check("the video choices go to the dl_add_ytdlp call only", perCalls[1].args.quality === "720p" && perCalls[0].args.quality === undefined && perCalls[0].args.mode === undefined
    && perCalls[2].args.quality === undefined && perCalls[2].args.mode === undefined && perCalls.every((c) => c.args.dest === "D"));
  check("addCalls without per-tool choices is what it always was; the base is not touched", eq(pure.addCalls(gAll, perBase)[1].args, { dest: "D", urls: ["https://youtu.be/1"] }) && eq(perBase, { dest: "D" }));

  // ---- a question the hub asked back
  const plAnswer = { ok: false, needs_clarification: true, queued: 0, message: "That link is a playlist", playlist: { title: "T", count: 3, cap: 50 }, call: { tool: "dl_add_ytdlp" } };
  const fu = pure.followUpFor(plAnswer, { tool: "dl_add_ytdlp", urls: ["https://youtube.com/playlist?list=1"] });
  check("a playlist for one link offers 'playlist: all' as a button", fu.label === "Add the playlist (up to 50 videos)" && same(fu.extra, { playlist: "all" }) && fu.tool === "dl_add_ytdlp" && fu.message === "That link is a playlist");
  check("a playlist among several links offers only the words", pure.followUpFor(plAnswer, { tool: "dl_add_ytdlp", urls: ["a", "b"] }).label === "");
  check("a page only the generic extractor might read offers generic=true for one link, and only when the hub says it is available",
    same(pure.followUpFor({ needs_clarification: true, generic_available: true, call: {}, message: "m" }, { tool: "dl_add_ytdlp", urls: ["u"] }).extra, { generic: true })
    && pure.followUpFor({ needs_clarification: true, generic_available: false, call: {}, message: "m" }, { tool: "dl_add_ytdlp", urls: ["u"] }).label === "");
  const tf = pure.followUpFor({ needs_clarification: true, torrent: { files: 30 }, message: "30 files" }, { tool: "dl_add_stremio", urls: [MAGNET] });
  check("a torrent that asked which files offers the file list", tf.label === "Choose files\u2026" && tf.tool === "" && tf.source === MAGNET);
  check("a question with no message still has words", pure.followUpFor({ needs_clarification: true }, { tool: "dl_add", urls: ["u"] }).message.length > 10);

  // ---- what an add came to
  const okOne = { ok: true, count: 1, added: [{ id: "h_1", duplicate: false, start_at: null }], skipped: [] };
  const d1 = pure.describeAdd([{ tool: "dl_add", urls: ["https://a.example/x.zip"], result: okOne }], {});
  check("one link added: 'Added 1 download', and the new row is selected", d1.tone === "ok" && d1.headline === "Added 1 download" && d1.select === "h_1" && d1.lines.length === 0 && d1.keep.length === 0 && d1.queued === 1);
  const dSched = pure.describeAdd([{ tool: "dl_add", urls: ["u"], result: { ok: true, count: 1, added: [{ id: "h_1", start_at: "2026-10-28T03:00:00" }] } }], {});
  check("a scheduled add says when it starts", dSched.tone === "ok" && dSched.headline === "Scheduled 1 download, will start at 2026-10-28 03:00" && dSched.when === "2026-10-28 03:00");
  const dPaused = pure.describeAdd([{ tool: "dl_add", urls: ["a", "b", "c"], result: { ok: true, count: 3, added: [{ id: "1" }, { id: "2" }, { id: "3" }] } }], { paused: true });
  check("a paused add says it waits for you", dPaused.headline === "Added 3 downloads, paused: start them from the list when you are ready" && dPaused.paused === true && dPaused.select === "");
  check("one paused download is 'it'", pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: okOne }], { paused: true }).headline === "Added 1 download, paused: start it from the list when you are ready");
  check("a start time wins over 'paused'", pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: { ok: true, count: 1, added: [{ id: "1", start_at: "2026-10-28T03:00:00" }] } }], { paused: true }).paused === false);
  check("links already in the list are counted", pure.describeAdd([{ tool: "dl_add", urls: ["a", "b"], result: { ok: true, count: 2, added: [{ id: "1" }, { id: "2", duplicate: true }] } }], {}).headline === "Added 2 downloads (1 already in the list)");
  const dPart = pure.describeAdd([{ tool: "dl_add", urls: ["a", "b"], result: { ok: true, count: 1, added: [{ id: "h_1" }], skipped: [{ input: "b", problem: "private address" }] } }], {});
  check("partial: some queued, one refused with its reason, the refused one stays in the box", dPart.tone === "partial" && dPart.headline === "Added 1 of 2 links" && eq(dPart.lines, [{ url: "b", text: "private address" }]) && eq(dPart.keep, ["b"]) && dPart.select === "");
  const dTwo = pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: okOne }, { tool: "dl_add_ytdlp", urls: ["v1", "v2"], result: { ok: false, error: "yt-dlp isn't installed" } }], {});
  check("partial across tools: the failed tool's links each carry its reason and stay in the box", dTwo.tone === "partial" && dTwo.headline === "Added 1 of 3 links" && dTwo.lines.length === 2
    && dTwo.lines.every((l) => l.text === "yt-dlp isn't installed") && eq(dTwo.keep, ["v1", "v2"]));
  const dBad = pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: { ok: false, error: "boom" } }], {});
  check("a refused write: nothing added, the reason, the link stays", dBad.tone === "bad" && dBad.headline === "Nothing was added." && eq(dBad.lines, [{ url: "a", text: "boom" }]) && eq(dBad.keep, ["a"]) && dBad.queued === 0);
  const dNo = pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: { ok: false, cancelled: true, error: "not confirmed: nothing was queued" } }], {});
  check("a declined question is neutral, not an error, and says what the hub said", dNo.tone === "info" && dNo.lines[0].text === "not confirmed: nothing was queued"
    && pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: { ok: false, declined: true } }], {}).lines[0].text === "You did not confirm.");
  const dStop = pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: { ok: false, declined: true, error: "no" } }, { tool: "dl_add_ytdlp", urls: ["b"], result: null, why: "Not tried: you declined the question before it." }], {});
  check("links not tried after a decline are listed with why and stay in the box", dStop.tone === "info" && dStop.lines.length === 2 && dStop.lines[1].text.indexOf("Not tried") === 0 && eq(dStop.keep, ["a", "b"]));
  const dAsk = pure.describeAdd([{ tool: "dl_add_ytdlp", urls: ["https://youtube.com/playlist?list=1"], result: plAnswer }], {});
  check("a question the hub asked: nothing added yet, the button is offered, the link stays", dAsk.tone === "ask" && dAsk.headline === "Nothing was added yet: it needs your answer." && dAsk.asks.length === 1 && dAsk.asks[0].label.indexOf("Add the playlist") === 0 && dAsk.lines.length === 0 && dAsk.keep.length === 1);
  check("a question beside something added is a partial add", pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: okOne }, { tool: "dl_add_ytdlp", urls: ["p"], result: plAnswer }], {}).tone === "partial");
  check("the hub's note (the daemon is off, an option was ignored) comes along", pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: Object.assign({ note: "The download hub daemon isn't running" }, okOne) }], {}).note === "The download hub daemon isn't running");
  check("two answers add up", pure.describeAdd([{ tool: "dl_add", urls: ["a"], result: okOne }, { tool: "dl_add_stremio", urls: ["m"], result: { ok: true, count: 1, added: [{ id: "h_2" }] } }], {}).headline === "Added 2 downloads");
  check("no calls at all", pure.describeAdd([], {}).tone === "bad" && pure.describeAdd(null, null).total === 0);
  check("the links left in the box: untouched ones and the ones not added", eq(pure.remainingLinks(["a", "b", "c"], [{ urls: ["a", "b"] }], ["b"]), ["b", "c"]) && eq(pure.remainingLinks(["a", "b"], [], []), ["a", "b"]) && eq(pure.remainingLinks(["a", "b", "c"], [{ urls: ["a", "b"] }]), ["c"]));
  check("start times from the hub in words", pure.startWords("2026-10-28T03:00:00") === "2026-10-28 03:00" && pure.startWords("later") === "later" && pure.startWords(null) === "");
  check("the old one-answer merge still behaves (describeAdd uses it for the counts)", pure.mergeAddResults([okOne, okOne]).count === 2);

  // ---- the form in the page: the source, the markup and the style
  const html9 = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
  const css9 = fs.readFileSync(path.join(ui, "tool.css"), "utf8");
  const formHtml = html9.slice(html9.indexOf('<form class="add"'), html9.indexOf("</form>", html9.indexOf('<form class="add"')));
  const ids9 = [...html9.matchAll(/\bid="([A-Za-z0-9_-]+)"/g)].map((x) => x[1]);
  check("no element id appears twice in tool.html", new Set(ids9).size === ids9.length);
  check("every id other code and tests use is still there", ["addForm", "addInput", "addHint", "addChoose", "addDest", "addDestBrowse", "addDestHelp", "addStart", "addLimit", "addLimitUnit", "addOpen", "addNotify", "addChannels", "addEvents", "addGroup", "addPaused", "addBtn", "picker"].every((id) => ids9.indexOf(id) !== -1));
  check("the five option groups exist and all start closed", ["addWhere", "addWhen", "addSpeed", "addTell", "addBatch"].every((id) => new RegExp('<details class="af-group" id="' + id + '">').test(formHtml)) && !/<details[^>]*\bopen\b/.test(formHtml));
  check("the torrent row, the video block and the result start hidden", /id="addTorrent" hidden/.test(formHtml) && /<fieldset class="af-box" id="addVideo" hidden>/.test(formHtml) && /id="addResult" role="status" aria-live="polite" hidden/.test(formHtml));
  check("the hint line has screen-reader text: the visible line is hidden from the reader, a live region says it in words", /id="addHint" aria-hidden="true"/.test(formHtml) && /class="af-sr" id="addHintSr" role="status" aria-live="polite"/.test(formHtml)
    && /aria-describedby="addHelp addHintSr"/.test(formHtml));
  const controls = [...formHtml.matchAll(/<(input|select|textarea)\b[^>]*>/g)];
  const labelled = (m) => {
    const id = (/\bid="([^"]+)"/.exec(m[0]) || [0, ""])[1];
    if (/\baria-label="[^"]+"/.test(m[0])) return true;
    if (id && new RegExp('<label[^>]*\\bfor="' + id + '"').test(formHtml)) return true;
    const before = formHtml.slice(0, m.index);
    return before.lastIndexOf("<label") > before.lastIndexOf("</label>");
  };
  check("every control in the form has a label tied to it (for=, wrapping, or aria-label)", controls.length > 20 && controls.every(labelled), controls.filter((m) => !labelled(m)).map((m) => m[0]).join(" | "));
  check("every aria-describedby points at something in the page", [...formHtml.matchAll(/aria-describedby="([^"]+)"/g)].every((m) => m[1].split(/\s+/).every((id) => ids9.indexOf(id) !== -1)));
  check("the notification groups are named by a visible label", /id="addChannels" role="group" aria-labelledby="addChannelsLabel"/.test(formHtml) && /id="addEvents" role="group" aria-labelledby="addEventsLabel"/.test(formHtml));
  check("the speed unit box has a name", /id="addLimitUnit" aria-label="Speed unit"/.test(formHtml));
  const addCss = css9.slice(css9.indexOf("/* ---- the add form (9C)"), css9.indexOf(".af-result__note"));
  check("the add form's style uses the --h-* tokens only: no colour literal, no other variable", addCss.length > 2000 && !/#[0-9a-fA-F]{3,8}\b/.test(addCss) && !/\brgba?\(|\bhsla?\(/.test(addCss)
    && [...addCss.matchAll(/var\((--[a-z0-9-]+)/g)].every((m) => /^--h-/.test(m[1])));
  check("it narrows: the video grid wraps by itself, the locked fieldset can shrink, long text breaks", /\.af-grid \{[^}]*auto-fit, minmax\(140px, 1fr\)/.test(addCss) && /\.af-lock \{[^}]*min-width: 0/.test(addCss) && /overflow-wrap: anywhere/.test(addCss));
  check("keyboard focus stays visible (the panel's own rule covers summary, button, input, select, textarea)", /button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible/.test(css9));
  check("the old .hint and .advanced rules are gone with the markup that used them", !/^\.hint\b/m.test(css9) && !/^\.advanced\b/m.test(css9) && !/class="(hint|advanced)\b/.test(html9));

  const form9 = source.slice(source.indexOf("function readAddForm"), source.indexOf("function resetAddOneOffs"));
  const submit9 = source.slice(source.indexOf("async function submitAdd"), source.indexOf("// ---- the add form's extras"));
  check("the form reads its choices once and sends the video ones to dl_add_ytdlp only", /perTool: \{ dl_add_ytdlp: videoOptionsFromForm\(videoFields\(\)\)\.args \}/.test(form9)
    && !/args\.(mode|quality|container|video_codec|audio_format|audio_quality|sub_langs|playlist|generic|engine)\b/.test(source.replace(/\/\/[^\n]*/g, "")));
  check("a folder, a batch name and 'paused' are still read the way they were", /args\.dest = dest/.test(form9) && /args\.group = group/.test(form9) && /args\.start_paused = true/.test(form9));
  check("one add at a time: a second press does nothing, and the form is locked and says 'Adding' while it runs", /if \(addBusy\) return;/.test(submit9) && /setAddBusy\(true\)/.test(submit9) && /finally \{\s*setAddBusy\(false\)/.test(submit9)
    && /\$\("#addLock"\)\.disabled = addBusy/.test(source) && /"Adding\\u2026"/.test(source) && /aria-busy/.test(source));
  check("the dry run goes ten links at a time to dl_add, and the grouping uses its answer", /async function dryRunAll[\s\S]{0,400}callTool\("dl_add", \{ urls: part, dry_run: true \}\)/.test(source) && /chunked\(urls, DRY_RUN_CHUNK\)/.test(source)
    && /const checked = await dryRunAll\(form\.urls\)/.test(submit9) && /groupByAddTool\(form\.urls, checked\.decisions\)/.test(submit9) && /addCalls\(groupByAddTool\(form\.urls, checked\.decisions\), form\.args, form\.perTool\)/.test(submit9));
  check("a torrent that asks which files still opens the picker, and the calls after it say they were not tried", /openPicker\(c\.args\.magnet, ""\)/.test(submit9) && /Not tried yet: the torrent above needs your choice first\./.test(submit9));
  check("a refused speed opens its group and focuses the field", /form\.error/.test(submit9) && /openAddGroup\(form\.group\)/.test(submit9) && /\$\(form\.focus\)\.focus\(\)/.test(submit9));
  check("what was not added stays in the box; a failed or partial add opens the groups that hold choices", /remainingLinks\(allUrls, entries, model\.keep\)/.test(source) && /openGroupsWithValues\(\)/.test(source));
  check("Ctrl+Enter, the folder picker, the torrent file picker and the dropped links are still wired", /e\.ctrlKey \|\| e\.metaKey/.test(source) && /\$\("#addForm"\)\.requestSubmit\(\)/.test(source) && /picker\.open\(\{ kind: "folder"/.test(source)
    && /\$\("#addChoose"\)\.addEventListener\("click"/.test(source) && /\$\("#addInput"\)\.value = urls\.join\("\\n"\);\s*previewHint\(urls\);/.test(source));
  check("a dropped link refreshes the whole form (previewHint does it), not just the line under the box", /async function previewHint[\s\S]{0,300}refreshAddUi\(\)/.test(source));
  check("every piece of link text, reason and file name is drawn with textContent only", !/innerHTML|insertAdjacentHTML|outerHTML/.test(source));
  check("the form talks only to tools of download_hub.py", ["dl_add", "dl_add_ytdlp", "dl_add_stremio"].every((n) => py2.includes('"' + n + '": tool_' + n)) && !/callTool\("dl_add_http"/.test(source));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("FAILED  threw: " + (e && e.stack || e)); process.exit(1); });
