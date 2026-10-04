// Verifies web/public/tool-manager.js's pure helpers (JarvisToolManager._pure):
// row building, filtering, grouping, the "what does this switch do" copy, name
// and import validation, validation-stage mapping and parameter listing.
//
// Same technique as verify_daemons_panel.js: the file is loaded with Node's vm
// into a bare `window`, nothing under test touches the DOM, so no jsdom.
//
//   node tests/verify_tool_manager.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = { localStorage: { getItem() { return null; }, setItem() {} } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "web", "public", "tool-manager.js"), "utf8"), { window: win, console });
const P = win.JarvisToolManager && win.JarvisToolManager._pure;
assert.ok(P, "window.JarvisToolManager._pure did not load");

let n = 0;
function ok(name, cond) { n += 1; if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }

const TOOLS = [
  { name: "click", source: "builtin", group: "desktop", file: "", description: "Click the mouse",
    confirm_required: true, ai_review: false, approval_summary: true,
    defaults: { confirm_required: true, ai_review: false, approval_summary: true }, parameters: {} },
  { name: "get_battery", source: "builtin", group: "core", confirm_required: false, ai_review: false, approval_summary: false,
    defaults: { confirm_required: false, ai_review: false, approval_summary: false } },
  { name: "mcp_list_servers", source: "mcp", group: "mcp", file: "mcp_tools.py", confirm_required: false, ai_review: false, approval_summary: false },
  { name: "hello", source: "user", group: "custom", file: "hello_world.py", description: "Say hello",
    confirm_required: false, ai_review: true, approval_summary: false,
    defaults: { confirm_required: false, ai_review: false, approval_summary: false } },
  { name: "run_shell", source: "auto", group: "dev_agent", file: "code_agent.py", disabled: true,
    confirm_required: true, ai_review: false, approval_summary: true,
    defaults: { confirm_required: true, ai_review: false, approval_summary: true } },
  { name: "search_tools", source: "builtin", group: "discovery", protected: "Discovery: without it the model can't find tools.",
    confirm_required: false, ai_review: false, approval_summary: false },
  { name: "click", source: "auto", group: "other" },                 // duplicate name: first wins
  { description: "no name" },                                         // unusable
];
const FILES = [
  { name: "hello_world", file: "hello_world.py", valid: true, enabled: true, tools: ["hello"], group: "custom", size: 400 },
  { name: "text_bits", file: "text_bits.py.disabled", valid: true, enabled: false, tools: ["shout_text", "whisper_text"], group: "custom", description: "x" },
  { name: "broken_one", file: "broken_one.py", valid: false, enabled: true, tools: [], error: "line 1: invalid syntax" },
  { name: "late", file: "late.py", valid: true, enabled: true, tools: ["not_in_catalogue"], group: "custom" },
  null,
];
const COMMANDS = {
  "deploy-prod": { description: "Ship it", run: ["git pull", { run: "npm run build" }], confirm_required: true, ai_review: false,
                   vars: { env: { description: "Target" }, tag: { description: "Tag", default: "latest" } } },
  "list-files": { description: "List files", run: "dir" },
};
const rows = P.buildRows(TOOLS, FILES, COMMANDS, ["deploy-prod"]);
const by = (name) => rows.find((r) => r.name === name);

// --- buildRows ---------------------------------------------------------------
ok("buildRows drops nameless and duplicate tools", rows.filter((r) => r.name === "click").length === 1 && !rows.some((r) => !r.name));
ok("first occurrence of a duplicate wins", by("click").group === "desktop");
ok("a loaded tool carries its three flags", by("click").flags.confirm_required && !by("click").flags.ai_review && by("click").flags.approval_summary);
ok("a loaded tool carries its defaults", by("click").defaults.approval_summary === true);
ok("a loaded user tool is linked to its file", by("hello").userFile && by("hello").userFile.file === "hello_world.py");
ok("a switched-off FILE contributes one 'fileoff' row per tool", by("shout_text").state === "fileoff" && by("whisper_text").state === "fileoff");
ok("a loaded tool you switched off is 'off' (and keeps its safeguards)", by("run_shell").state === "off" && by("run_shell").flags.confirm_required === true);
ok("a protected tool carries its reason", by("search_tools").protectedReason.startsWith("Discovery") && by("click").protectedReason === "");
ok("saved commands become 'command' rows in their own group", by("list-files").kind === "command" && by("list-files").group === "commands" && by("list-files").source === "command");
ok("a command in the disabled list is 'off', the rest 'loaded'", by("deploy-prod").state === "off" && by("list-files").state === "loaded");
ok("command rows have no tool safeguards (those are edited in the Commands panel)", by("deploy-prod").flags === null);
ok("a command and a tool with the same name don't collide", P.buildRows([{ name: "x", source: "builtin" }], [], { x: {} }, []).length === 2);
ok("buildRows tolerates bad commands input", P.buildRows([], [], null, null).length === 0 && P.buildRows([], [], [1, 2], undefined).length === 0 && P.buildRows([], [], { a: null }, []).length === 1);
ok("disabled rows have no flags (can't be switched)", by("shout_text").flags === null);
ok("a rejected file becomes a 'failed' row in its own group", by("broken_one.py").state === "failed" && by("broken_one.py").group === "__failed");
ok("a failed row keeps the validator's reason", by("broken_one.py").error === "line 1: invalid syntax");
ok("an enabled, valid file whose tool never loaded is 'pending'", by("not_in_catalogue").state === "pending");
ok("buildRows tolerates null / non-array input", P.buildRows(null, undefined).length === 0 && P.buildRows("x", 5).length === 0);

// --- bucketOf / labels -----------------------------------------------------
ok("builtin and auto both read as Shipped", P.bucketOf("builtin") === "shipped" && P.bucketOf("auto") === "shipped");
ok("user and mcp are their own buckets", P.bucketOf("user") === "user" && P.bucketOf("mcp") === "mcp");
ok("an unknown source falls back to shipped, not undefined", P.bucketOf("???") === "shipped");
ok("groupLabel prettifies ids", P.groupLabel("dev_agent") === "dev agent" && P.groupLabel("__failed") === "Failed to load");

// --- filters --------------------------------------------------------------
ok("search matches name", P.filterRows(rows, { search: "batt" }).map((r) => r.name).join() === "get_battery");
ok("search matches description, case-insensitively", P.filterRows(rows, { search: "SAY HELLO" }).some((r) => r.name === "hello"));
ok("search matches the owning file", P.filterRows(rows, { search: "mcp_tools" }).some((r) => r.name === "mcp_list_servers"));
ok("search matches a failed file's error text", P.filterRows(rows, { search: "invalid syntax" }).some((r) => r.name === "broken_one.py"));
ok("source=user keeps only your tools and files", P.filterRows(rows, { source: "user" }).every((r) => P.bucketOf(r.source) === "user"));
ok("source=attention = failed + pending only (switching something off on purpose isn't a problem)",
   P.filterRows(rows, { source: "attention" }).map((r) => r.state).sort().join() === "failed,pending");
ok("source=off = tools you switched off + tools whose file is off + commands you switched off",
   P.filterRows(rows, { source: "off" }).map((r) => r.name).sort().join() === "deploy-prod,run_shell,shout_text,whisper_text");
ok("source=command keeps only saved commands", P.filterRows(rows, { source: "command" }).every((r) => r.kind === "command") && P.filterRows(rows, { source: "command" }).length === 2);
ok("searching finds a command by its description", P.filterRows(rows, { search: "ship it" }).map((r) => r.name).join() === "deploy-prod");
ok("isOff / needsAttention agree with the states", P.isOff(by("run_shell")) && P.isOff(by("shout_text")) && !P.isOff(by("click")) && P.needsAttention(by("broken_one.py")) && !P.needsAttention(by("run_shell")));
ok("group filter narrows", P.filterRows(rows, { group: "desktop" }).map((r) => r.name).join() === "click");
ok("safeguard=confirm_required", P.filterRows(rows, { safeguard: "confirm_required" }).map((r) => r.name).sort().join() === "click,run_shell");
ok("safeguard=ai_review", P.filterRows(rows, { safeguard: "ai_review" }).map((r) => r.name).join() === "hello");
ok("safeguard=none excludes tools with any switch on", !P.filterRows(rows, { safeguard: "none" }).some((r) => r.name === "click" || r.name === "hello"));
ok("safeguard filters never match rows with no flags", !P.filterRows(rows, { safeguard: "none" }).some((r) => r.flags === null));
ok("safeguard=changed finds tools that differ from their default", P.filterRows(rows, { safeguard: "changed" }).map((r) => r.name).join() === "hello");
ok("filters combine (AND)", P.filterRows(rows, { source: "user", safeguard: "ai_review", search: "hello" }).length === 1);

// --- groupRows -----------------------------------------------------------------
const groups = P.groupRows(rows);
ok("failed files group comes first", groups[0].id === "__failed");
ok("remaining groups are alphabetical", groups.slice(1).map((g) => g.id).join() === groups.slice(1).map((g) => g.id).sort().join());
ok("rows inside a group are alphabetical", P.groupRows(rows).find((g) => g.id === "custom").rows.map((r) => r.name).join() === "hello,not_in_catalogue,shout_text,whisper_text");

// --- coverage -------------------------------------------------------------------
const cov = P.coverage(rows);
ok("coverage counts only loaded tools (not switched-off ones, not commands)", cov.total === 5);
ok("coverage per safeguard ignores a switched-off tool's flags", cov.confirm_required === 1 && cov.ai_review === 1 && cov.approval_summary === 1);
ok("coverage by source", cov.bySource.shipped === 3 && cov.bySource.mcp === 1 && cov.bySource.user === 1);
ok("coverage.none = loaded tools with no switch on", cov.none === 3);   // get_battery, mcp_list_servers, search_tools
ok("coverage reports what is off", cov.off === 3 && cov.commands === 2 && cov.commandsOff === 1);
ok("offSummary words it", P.offSummary(cov) === "3 tools, 1 saved command" && P.offSummary({ off: 1, commandsOff: 0 }) === "1 tool" && P.offSummary({ off: 0, commandsOff: 2 }) === "2 saved commands" && P.offSummary({ off: 0, commandsOff: 0 }) === "");
ok("coverage of nothing is all zeros", P.coverage([]).total === 0);

// --- the copy: it must match what the executor really does -------------------
ok("no switches: runs straight away", P.callOutcome({ confirm_required: false, ai_review: false }).id === "free");
ok("confirm only: asks", P.callOutcome({ confirm_required: true, ai_review: false }).id === "ask");
ok("AI overview with confirm: asks with a risk note", P.callOutcome({ confirm_required: true, ai_review: true }).id === "ai");
ok("AI overview ALONE still asks (ai_client prompts when either is on) and says so",
   P.callOutcome({ confirm_required: false, ai_review: true }).id === "ai" && /even with .Ask before running. off/.test(P.callOutcome({ confirm_required: false, ai_review: true }).text));
ok("callOutcome of a tool with no flags is null", P.callOutcome(null) === null);
ok("job copy: on = listed and covered", /lists it/.test(P.jobOutcome({ flags: { approval_summary: true }, defaults: { approval_summary: true } })));
ok("job copy: off for a normally-listed tool warns it can't run unattended",
   /not allowed to run it unattended/.test(P.jobOutcome({ flags: { approval_summary: false }, defaults: { approval_summary: true } })));
ok("job copy: off for an unflagged tool is just 'normal policy'",
   /normal policy/.test(P.jobOutcome({ flags: { approval_summary: false }, defaults: { approval_summary: false } })));
ok("changedKeys lists differing switches only", P.changedKeys(by("hello")).join() === "ai_review" && P.changedKeys(by("click")).length === 0);
ok("changedKeys is empty when defaults are unknown", P.changedKeys({ flags: { ai_review: true }, defaults: null }).length === 0);

// --- switching off: the words, the dependents list, commands ------------------------
ok("availabilityHint: a tool is off for the model but runnable by the owner", /can.t see this tool or use it/.test(P.availabilityHint(by("click"))) && /Debug/.test(P.availabilityHint(by("click"))));
ok("availabilityHint: a command mentions scheduled jobs failing", /saved command/.test(P.availabilityHint(by("list-files"))) && /fails with a clear message/.test(P.availabilityHint(by("list-files"))));
ok("offOutcome differs for tools and commands", P.offOutcome(by("run_shell")) !== P.offOutcome(by("deploy-prod")) && /Jarvis doesn.t know this command exists/.test(P.offOutcome(by("deploy-prod"))));
ok("dependentLines: a bullet per job with its next run", P.dependentLines([{ id: "a1", title: "Nightly", next_run: "2026-10-04T03:00:00" }, { id: "a2" }]).join("|") === "• Nightly — next 2026-10-04 03:00|• a2");
ok("dependentLines tolerates junk", P.dependentLines(null).length === 0 && P.dependentLines([null]).length === 1);
ok("commandSteps flattens strings and {run} steps", P.commandSteps(COMMANDS["deploy-prod"]).join("|") === "git pull|npm run build" && P.commandSteps(COMMANDS["list-files"]).join() === "dir" && P.commandSteps({}).length === 0);
const cv = P.commandVars(COMMANDS["deploy-prod"]);
ok("commandVars: required vs defaulted", cv[0].name === "env" && !cv[0].hasDefault && cv[1].hasDefault && cv[1].def === "latest" && P.commandVars(null).length === 0);

// --- names ----------------------------------------------------------------------
ok("nameProblem: empty", /name/i.test(P.nameProblem("", [], "new")));
ok("nameProblem: uppercase, spaces and punctuation", P.nameProblem("My Tool", [], "new") !== "" && P.nameProblem("tool-1", [], "new") !== "");
ok("nameProblem: must start with a letter", P.nameProblem("1tool", [], "new") !== "");
ok("nameProblem: 49 chars is the limit", P.nameProblem("a".repeat(49), [], "new") === "" && P.nameProblem("a".repeat(50), [], "new") !== "");
ok("nameProblem: an existing file is refused when creating", /already exists/.test(P.nameProblem("hello_world", ["hello_world"], "new")));
ok("nameProblem: …but not when editing that very file", P.nameProblem("hello_world", ["hello_world"], "edit") === "");
ok("importNameFromFile: lower-snake from a messy file name", P.importNameFromFile("My Tool-v2.py") === "my_tool_v2");
ok("importNameFromFile: strips directories and .py.disabled", P.importNameFromFile("C:\\Users\\me\\Downloads\\Cool.Tool.py") === "cool_tool" && P.importNameFromFile("x.py.disabled") === "x");
ok("importNameFromFile: a leading digit gets a prefix", P.importNameFromFile("2fast.py") === "tool_2fast");
ok("importNameFromFile: result passes nameProblem", P.nameProblem(P.importNameFromFile("My Tool-v2.py"), [], "new") === "");
ok("importNameFromFile: nothing usable gives ''", P.importNameFromFile("!!!.py") === "");
ok("importProblem: only .py", P.importProblem({ name: "x.txt" }) !== "" && P.importProblem({ name: "x.PY" }) === "" && P.importProblem(null) !== "");
ok("sourceProblem: empty, huge, binary", P.sourceProblem("  \n") !== "" && P.sourceProblem("x".repeat(P.MAX_SOURCE_CHARS + 1)) !== "" && P.sourceProblem("a\u0000b") !== "");
ok("sourceProblem: ordinary code passes", P.sourceProblem("def f():\n    pass\n") === "");

// --- validation display ----------------------------------------------------------
ok("parseErrorLine reads the validator's wording", P.parseErrorLine("line 12: invalid syntax") === 12);
ok("parseErrorLine is null when there is none", P.parseErrorLine("TOOLS must be a non-empty dict") === null && P.parseErrorLine(null) === null);
ok("stages: nothing run yet", P.stageStates(null).join() === "idle,idle,idle");
ok("stages: ok", P.stageStates({ ok: true }).join() === "ok,ok,ok");
ok("stages: syntax failure skips the rest", P.stageStates({ ok: false, stage: "syntax" }).join() === "bad,skip,skip");
ok("stages: an empty file is a syntax-stage failure", P.stageStates({ ok: false, stage: "empty" }).join() === "bad,skip,skip");
ok("stages: import failure passes syntax", P.stageStates({ ok: false, stage: "import" }).join() === "ok,bad,skip");
ok("stages: contract failure passes the first two", P.stageStates({ ok: false, stage: "contract" }).join() === "ok,ok,bad");
ok("stages: an unknown stage marks all bad rather than inventing progress", P.stageStates({ ok: false, stage: "weird" }).join() === "bad,bad,bad");

// --- misc ---------------------------------------------------------------------
ok("lineCount", P.lineCount("") === 1 && P.lineCount("a\nb\nc") === 3);
ok("formatBytes", P.formatBytes(500) === "500 B" && P.formatBytes(1536) === "1.5 KB" && P.formatBytes(20480) === "20 KB");
ok("parseArgs: blank means {}", P.parseArgs("  ").ok && Object.keys(P.parseArgs("").value).length === 0);
ok("parseArgs: rejects bad JSON, arrays and scalars", !P.parseArgs("{").ok && !P.parseArgs("[1]").ok && !P.parseArgs("3").ok && !P.parseArgs("null").ok);
ok("parseArgs: accepts an object", P.parseArgs('{"a": 1}').value.a === 1);
const pl = P.paramList({ properties: { x: { type: "integer", description: "X." }, b: { type: "string", enum: ["l", "r"] }, n: { type: ["string", "null"] } }, required: ["x"] });
ok("paramList reads name/type/required/description", pl[0].name === "x" && pl[0].type === "integer" && pl[0].required && pl[0].description === "X.");
ok("paramList shows enums as the type and drops null from unions", pl[1].type === "l | r" && pl[2].type === "string" && !pl[1].required);
ok("paramList of nothing is empty", P.paramList(null).length === 0 && P.paramList({}).length === 0);
ok("the safeguards keys line up with tool_safety.VALID_KEYS", P.KEYS.join() === "confirm_required,ai_review,approval_summary");
ok("NAME_RE matches the server's CTOOL_NAME_RE", String(P.NAME_RE) === String(/^[a-z][a-z0-9_]{0,48}$/));

// --- no markup injection: the source must never use innerHTML ----------------------
const src = fs.readFileSync(path.join(__dirname, "..", "web", "public", "tool-manager.js"), "utf8");
ok("tool-manager.js never assigns innerHTML / outerHTML / insertAdjacentHTML", !/\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML/.test(src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "")));


// ---- L.43: Ask Jarvis reply splitting + tab numbering ----------------------
{
  const S = P.splitAgentReply;
  const code = 'TOOL_GROUP = "custom"\nTOOLS = {}\n';
  let r = S("Adding it.\n```python\n" + code + "```\nRead first.");
  ok("agent: note and code split", r.note.startsWith("Adding it.") && r.note.includes("Read first.") && r.code === code && r.complete === true);
  r = S("Which folder?");
  ok("agent: no fence is a question", r.code === "" && r.note === "Which folder?" && r.complete === false);
  r = S("On it.\n```python\nx = 1\ny = 2");
  ok("agent: streaming - open fence types what it has", r.code === "x = 1\ny = 2\n" && r.complete === false);
  r = S("On it.\n```python\nx = 1\n``");
  ok("agent: a half-typed closing fence never flashes into the code", r.code === "x = 1\n" && r.complete === false);
  r = S("n\n```python\nx = 1```");
  ok("agent: fence closed without a newline", r.code === "x = 1\n" && r.complete === true);
  r = S("n\n```py\nx = 1\n```");
  ok("agent: ```py is accepted", r.code === "x = 1\n");
  ok("agent: empty input is safe", S("").code === "" && S(null).note === "");
  ok("untitled numbering: smallest free", P.nextFree([]) === 1 && P.nextFree([1, 2, 4]) === 3 && P.nextFree([2]) === 1);
}

console.log(`\n${n} passed, 0 failed`);
