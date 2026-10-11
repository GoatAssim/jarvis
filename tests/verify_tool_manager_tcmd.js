// Verifies the Tool Manager's row + switch for tool commands (TOOL_COMMANDS, the `/` palette
// words a tool file ships): web/public/tool-manager.js's pure helpers, via
// JarvisToolManager._pure. Same technique as verify_tool_manager.js: the file is loaded with
// Node's vm into a bare `window`; nothing under test touches the DOM.
//
//   node tests/verify_tool_manager_tcmd.js
//
// Written, not run, by the author of the patch: the owner runs it.

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

const CMDS = [
  { name: "disk", tool: "disk_report", summary: "How full is each drive", riskTier: "safe", declaredTier: "safe", file: "disk_demo.py", usage: "[drive]", disabled: false, switched_off: false, disabled_by_tool: false },
  { name: "wipe", tool: "disk_wipe", summary: "Wipe a target", riskTier: "dangerous", declaredTier: "", file: "disk_demo.py", disabled: true, switched_off: true, disabled_by_tool: false, disabled_reason: "it is switched off in the Tool Manager" },
  { name: "two-a", tool: "two_a", summary: "Run A", riskTier: "caution", file: "two_tools.py", disabled: true, switched_off: false, disabled_by_tool: true, disabled_reason: "its tool two_a is switched off in the Tool Manager" },
  { summary: "no name" },
  null,
];

{
  const rows = P.buildRows([], [], null, [], { toolCommands: CMDS });
  const t = rows.filter((r) => r.kind === "tcmd");
  ok("one row per usable command (nameless and null ones dropped)", t.length === 3);
  ok("id, group and bucket", t[0].id === "k:disk" && t[0].group === "tool_commands" && t[0].source === "tcmd");
  ok("a command that can run is loaded; one that can't is off", t[0].state === "loaded" && t[1].state === "off" && t[2].state === "off");
  ok("the summary is the description", t[0].description === "How full is each drive");
  ok("the row keeps what the server said", t[2].extra.disabled_by_tool === true && t[1].extra.switched_off === true);
  ok("a command row is not mistaken for a tool or a saved command", t.every((r) => r.kind !== "tool" && r.kind !== "command") && t.every((r) => !r.flags));
  ok("ids can't collide with a saved command of the same name", P.buildRows([], [], { disk: { description: "x" } }, [], { toolCommands: CMDS }).filter((r) => r.name === "disk").map((r) => r.id).sort().join() === "c:disk,k:disk");
  ok("tolerates a missing / non-array command list", P.buildRows([], [], null, [], { toolCommands: "x" }).length === 0 && P.buildRows([], [], null, [], {}).length === 0);

  ok("tcmd is an extra kind, with a noun and a label", P.EXTRA_KINDS.includes("tcmd") && P.KIND_NOUN.tcmd === "tool command");
  ok("group label", P.groupLabel("tool_commands") === "Tool commands");
  ok("origin names the file and TOOL_COMMANDS", /disk_demo\.py/.test(P.originOf(t[0])) && /TOOL_COMMANDS/.test(P.originOf(t[0])));
  ok("the hint says the tool it runs keeps working", /palette/.test(P.availabilityHint(t[0])) && /keeps working/.test(P.availabilityHint(t[0])));
  ok("off by the owner's switch: the tool is untouched", /untouched/.test(P.offOutcome(t[1])) && !/because its tool/.test(P.offOutcome(t[1])));
  ok("off because its tool is off: says how to bring it back", /because its tool/.test(P.offOutcome(t[2])) && /returns/.test(P.offOutcome(t[2])));

  ok("matchesSource: the tcmd bucket", rows.filter((r) => P.matchesSource(r, "tcmd")).length === 3 && P.matchesSource(t[1], "off") && !P.matchesSource(t[0], "off"));
  ok("matchesSource: a tool command is not a saved command", !P.matchesSource(t[0], "command"));
  ok("search finds it by name, file and group", P.matchesSearch(t[1], "wipe") && P.matchesSearch(t[1], "disk_demo") && P.matchesSearch(t[1], "tool commands".replace(" ", "_")) && !P.matchesSearch(t[1], "zzz-nothing"));

  const cov = P.coverage(rows);
  ok("coverage counts the commands that are off", cov.extraOff.tcmd === 2 && cov.off === 0 && cov.commandsOff === 0);
  ok("offSummary words them", /2 tool commands/.test(P.offSummary(cov)) && /1 tool command\b/.test(P.offSummary({ off: 0, commandsOff: 0, extraOff: { tcmd: 1 } })));
  ok("offSummary leaves them out when none are off", !/tool command/.test(P.offSummary({ off: 1, commandsOff: 0, extraOff: { tcmd: 0 } })));
}

console.log(`\n${n} passed, 0 failed`);
