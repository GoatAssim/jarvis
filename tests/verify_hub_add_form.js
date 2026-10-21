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
  check("submitAdd asks the router with a dry run first", /callTool\("dl_add", \{ urls: urls, dry_run: true \}\)/.test(submit));
  check("submitAdd then calls the tool of each group", submit.includes("groupByAddTool(urls, pre && pre.decisions)") && /callTool\(c\.tool, c\.args\)/.test(submit));
  check("submitAdd no longer sends the links in its own argument object", /const args = \{\};/.test(submit));
  const named = new Set([...source.matchAll(/callTool\("([a-z_]+)"/g)].map((x) => x[1]));
  check("the literal tool names the panel calls are all in this file's tool list", [...named].every((n) => /^(dl_|hub_)/.test(n)));
  const py = fs.readFileSync(path.join(ui, "..", "download_hub.py"), "utf8");
  check("every add tool the panel can pick is a tool this file defines",
    ["dl_add", "dl_add_ytdlp", "dl_add_stremio"].every((n) => py.includes('"' + n + '": tool_' + n)));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("FAILED  threw: " + (e && e.stack || e)); process.exit(1); });
