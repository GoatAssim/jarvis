// Verifies web/public/mcp-servers.js's pure helpers (JarvisMcp._pure): state
// labels, relative time, "needs attention", search (a tool name finds its
// server), filter chips and counts, the status line, argument parsing and -
// the one that matters most - buildSpec, which decides what the panel sends
// and what a blank secret field means.
//
// Same technique as verify_notifications.js: loaded with Node's vm into a bare
// context with no `document`, so the module exports only its pure half. The
// browser behaviour is covered by tests/verify_mcp_servers_ui.py.
//
//   node tests/verify_mcp_servers.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = {};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "..", "web", "public", "mcp-servers.js"), "utf8"),
  { window: win, console, setTimeout, clearTimeout, Date }
);
const P = win.JarvisMcp && win.JarvisMcp._pure;
if (!P) throw new Error("window.JarvisMcp._pure did not load");

let n = 0;
let failed = 0;
function ok(name, cond, detail) {
  n += 1;
  if (!cond) { failed += 1; console.log(`FAILED   ${name}${detail === undefined ? "" : ": " + JSON.stringify(detail)}`); }
  else console.log(`ok       ${name}`);
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// --- state labels ---------------------------------------------------------------
for (const [state, label, tone] of [
  ["ok", "Ready", "ok"], ["stale", "Tool list is old", "warn"], ["not_refreshed", "Not connected yet", "warn"],
  ["error", "Error", "bad"], ["misconfigured", "Needs fixing", "bad"], ["disabled", "Off", "off"],
]) {
  const s = P.stateInfo(state);
  ok(`state ${state} reads "${label}"`, s.label === label && s.tone === tone, s);
}
ok("an unknown state is shown as its own word, not hidden", P.stateInfo("weird").label === "weird");
ok("a missing state doesn't throw", P.stateInfo(undefined).label === "unknown");

// --- relative time (seconds in, milliseconds for now) ---------------------------------
const NOW = 1_800_000_000_000;
const ago = (secs) => P.relativeTime(NOW / 1000 - secs, NOW);
ok("never when there is no time", P.relativeTime(null, NOW) === "never" && P.relativeTime(0, NOW) === "never");
ok("seconds ago reads just now", ago(5) === "just now");
ok("minutes", ago(600) === "10 min ago", ago(600));
ok("hours", ago(3 * 3600) === "3 h ago", ago(3 * 3600));
ok("days", ago(5 * 86400) === "5 d ago", ago(5 * 86400));
ok("a time in the future clamps instead of going negative", P.relativeTime(NOW / 1000 + 500, NOW) === "just now");

// --- needs attention -----------------------------------------------------------------
const srv = (o) => Object.assign({ name: "s", slug: "s", enabled: true, state: "ok", transport: "stdio",
                                   command: "npx", args: [], tools: [], description: "" }, o);
ok("an error needs attention", P.needsAttention(srv({ state: "error" })));
ok("misconfigured needs attention", P.needsAttention(srv({ state: "misconfigured" })));
ok("not refreshed needs attention", P.needsAttention(srv({ state: "not_refreshed" })));
ok("a stale list needs attention", P.needsAttention(srv({ state: "stale" })));
ok("ready does not", !P.needsAttention(srv({ state: "ok" })));
ok("a switched-off server never does, even with an old error", !P.needsAttention(srv({ enabled: false, state: "disabled", error: "x" })));
ok("a name collision does", P.needsAttention(srv({ renamed_due_to_collision: true })));
ok("null is safe", P.needsAttention(null) === false);

// --- command line --------------------------------------------------------------------
ok("command and args join with spaces", P.commandLine(srv({ command: "npx", args: ["-y", "pkg"] })) === "npx -y pkg");
ok("an argument with a space is quoted", P.commandLine(srv({ command: "node", args: ["my file.js"] })) === 'node "my file.js"');
ok("a remote server shows its (redacted) url", P.commandLine(srv({ transport: "http", url: "https://h/x?\u2026" })) === "https://h/x?\u2026");
ok("empty args are skipped", P.commandLine(srv({ command: "x", args: ["", "a"] })) === "x a");

// --- search ----------------------------------------------------------------------------
const git = srv({ name: "git", description: "Version control", tools: [{ name: "commit", description: "Record changes" }, { name: "log", description: "" }] });
const files = srv({ name: "files", command: "npx", args: ["server-filesystem"], tools: [{ name: "read_file", description: "Read a file" }] });
ok("an empty query matches everything", P.matchesQuery(git, "") && P.matchesQuery(git, "   "));
ok("a name matches", P.matchesQuery(git, "git"));
ok("a TOOL name finds its server", P.matchesQuery(git, "commit") && !P.matchesQuery(files, "commit"));
ok("a tool description matches", P.matchesQuery(files, "read a file"));
ok("every word must match somewhere (name + tool)", P.matchesQuery(git, "git commit") && !P.matchesQuery(git, "git nonsense"));
ok("matching is case-insensitive", P.matchesQuery(git, "VERSION"));
ok("the command line is searchable", P.matchesQuery(files, "filesystem"));
ok("a regex-looking query is just text, not a pattern", P.matchesQuery(git, "(") === false && P.matchesQuery(git, ".*") === false);

// --- chips ------------------------------------------------------------------------------
const list = [srv({ name: "a", state: "ok" }), srv({ name: "b", state: "error" }), srv({ name: "c", enabled: false, state: "disabled" })];
ok("chip counts", eq(P.chipCounts(list), { all: 3, attention: 1, on: 2, off: 1 }), P.chipCounts(list));
ok("the All chip keeps everything", P.filterServers(list, "", "all").length === 3);
ok("the attention chip", eq(P.filterServers(list, "", "attention").map((s) => s.name), ["b"]));
ok("the On chip", eq(P.filterServers(list, "", "on").map((s) => s.name), ["a", "b"]));
ok("the Off chip", eq(P.filterServers(list, "", "off").map((s) => s.name), ["c"]));
ok("a chip and a query combine", eq(P.filterServers(list, "b", "on").map((s) => s.name), ["b"]));
ok("filtering never mutates or reorders the input", eq(list.map((s) => s.name), ["a", "b", "c"]));
ok("an unknown chip behaves like All", P.filterServers(list, "", "???").length === 3);
ok("no servers is safe", P.filterServers(undefined, "x", "all").length === 0 && P.chipCounts(undefined).all === 0);

// --- status line -------------------------------------------------------------------------------
ok("no servers", P.statusLine({ servers: [] }) === "no servers yet" && P.statusLine(null) === "no servers yet");
ok("counts and tools", P.statusLine({ servers: [{ enabled: true, state: "ok" }, { enabled: false }], enabled_count: 1, total_tools: 4 }) === "1 on of 2 \u00b7 4 tools",
   P.statusLine({ servers: [{ enabled: true, state: "ok" }, { enabled: false }], enabled_count: 1, total_tools: 4 }));
ok("one tool is singular", /1 tool$/.test(P.statusLine({ servers: [{ enabled: true, state: "ok" }], enabled_count: 1, total_tools: 1 })));
ok("attention is called out", /1 needs attention/.test(P.statusLine({ servers: [{ enabled: true, state: "error" }], enabled_count: 1, total_tools: 0 })));
ok("several are plural", /2 need attention/.test(P.statusLine({ servers: [{ enabled: true, state: "error" }, { enabled: true, state: "stale" }], enabled_count: 2, total_tools: 0 })));

// --- parseArgs ---------------------------------------------------------------------------------------
ok("one argument per line, spaces kept", eq(P.parseArgs("-y\n@scope/pkg\nmy folder"), ["-y", "@scope/pkg", "my folder"]));
ok("blank lines and CRLF are tidied", eq(P.parseArgs("a\r\n\r\n  b  \r\n"), ["a", "b"]));
ok("empty is an empty list", eq(P.parseArgs(""), []) && eq(P.parseArgs(undefined), []));
ok("a quote is not interpreted (there is no shell)", eq(P.parseArgs('--name="x y"'), ['--name="x y"']));

// --- buildSpec -----------------------------------------------------------------------------------------
const local = (o) => Object.assign({ name: "files", transport: "stdio", command: "npx", argsText: "-y\npkg", cwd: "", url: "",
                                     description: "", enabled: true, trusted: false, env: [] }, o);
let b = P.buildSpec(local(), null);
ok("a good local form builds with no errors", b.errors.length === 0 && b.name === "files", b);
ok("it carries command, args and the two switches", b.spec.command === "npx" && eq(b.spec.args, ["-y", "pkg"]) && b.spec.enabled === true && b.spec.trusted === false, b.spec);
ok("no name is an error", P.buildSpec(local({ name: "  " }), null).errors.length === 1);
ok("a name of only punctuation is an error", P.buildSpec(local({ name: "!!!" }), null).errors.length === 1);
ok("a name starting with a dash is an error", P.buildSpec(local({ name: "-x" }), null).errors.length === 1);
ok("a 65-character name is an error", P.buildSpec(local({ name: "n".repeat(65) }), null).errors.length === 1);
ok("no command is an error", P.buildSpec(local({ command: " " }), null).errors.length === 1);
ok("name and command both missing gives two errors", P.buildSpec(local({ name: "", command: "" }), null).errors.length === 2);

b = P.buildSpec(local({ env: [{ key: "TOKEN", value: "abc" }, { key: "", value: "" }] }), null);
ok("an environment row is sent as NAME: value, and a blank row is ignored", eq(b.spec.env, { TOKEN: "abc" }), b.spec.env);
b = P.buildSpec(local({ env: [{ key: "TOKEN", value: "", existing: true }] }), { transport: "stdio" });
ok("a blank value on a STORED variable is null = keep what is stored", b.spec.env.TOKEN === null, b.spec.env);
b = P.buildSpec(local({ env: [{ key: "NEW", value: "", existing: false }] }), null);
ok("a blank value on a NEW variable is an empty string, not 'keep'", b.spec.env.NEW === "", b.spec.env);
ok("a bad variable name is an error", P.buildSpec(local({ env: [{ key: "A-B", value: "x" }] }), null).errors.length === 1);
ok("a duplicate variable name is an error", P.buildSpec(local({ env: [{ key: "A", value: "1" }, { key: "A", value: "2" }] }), null).errors.length === 1);
ok("a value with no name is an error", P.buildSpec(local({ env: [{ key: "", value: "x" }] }), null).errors.length === 1);

const remote = (o) => Object.assign(local({ transport: "http", command: "", argsText: "" }), o);
b = P.buildSpec(remote({ url: "https://h.example/mcp" }), null);
ok("a good remote form builds", b.errors.length === 0 && b.spec.url === "https://h.example/mcp" && b.spec.transport === "http", b);
ok("a remote spec has no command, args or env", !("command" in b.spec) && !("args" in b.spec) && !("env" in b.spec), b.spec);
ok("no URL on a NEW remote server is an error", P.buildSpec(remote({ url: "" }), null).errors.length === 1);
ok("a non-http URL is an error", P.buildSpec(remote({ url: "ftp://x" }), null).errors.length === 1);
b = P.buildSpec(remote({ url: "" }), { transport: "http", url_redacted: true });
ok("an EMPTY url on an edit whose stored url is hidden means keep (null)", b.errors.length === 0 && b.spec.url === null, b);
b = P.buildSpec(remote({ url: "" }), { transport: "http", url_redacted: false });
ok("an empty url on an edit with a plain stored url is still an error (it is shown in the box)", b.errors.length === 1);
b = P.buildSpec(remote({ url: "" }), { transport: "stdio", url_redacted: true });
ok("switching a local server to remote can't 'keep' a url it never had", b.errors.length === 1);
ok("the description is trimmed", P.buildSpec(local({ description: "  hi  " }), null).spec.description === "hi");

console.log(`\n${n - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
