// Verifies web/public/tool-manager.js's commandLines (JarvisToolManager._pure): the palette
// commands listed in the confirmation shown when a tool is switched off.
//
// Same technique as verify_tool_manager.js (vm into a bare `window`, no DOM).
//
//   node tests/verify_tool_manager_off_commands.js
//
// Written, not run, by the author of the patch: the owner runs it.

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = { localStorage: { getItem() { return null; }, setItem() {} } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "web", "public", "tool-manager.js"), "utf8"), { window: win, console });
const P = win.JarvisToolManager && win.JarvisToolManager._pure;
assert.ok(P && typeof P.commandLines === "function", "JarvisToolManager._pure.commandLines did not load");

let n = 0;
function ok(name, cond) { n += 1; if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }
const L = P.commandLines;

ok("one line per command, with a slash", JSON.stringify(L(["disk", "disk-wipe"])) === JSON.stringify(["\u2022 /disk", "\u2022 /disk-wipe"]));
ok("order is kept", L(["b", "a"])[0] === "\u2022 /b");
ok("duplicates and blanks are dropped, names are trimmed", JSON.stringify(L(["a", " a ", "", "  ", "b"])) === JSON.stringify(["\u2022 /a", "\u2022 /b"]));
ok("non-text entries are dropped", JSON.stringify(L([null, 5, {}, "ok"])) === JSON.stringify(["\u2022 /ok"]));
ok("empty / missing / not-a-list gives no lines", L([]).length === 0 && L(null).length === 0 && L(undefined).length === 0 && L("x").length === 0 && L({}).length === 0);
const many = Array.from({ length: 15 }, (_, i) => "c" + i);
const m = L(many);
ok("a long list is cut at 12 with a count of the rest", m.length === 13 && m[11] === "\u2022 /c11" && /and 3 more/.test(m[12]));
ok("exactly 12 is not cut", L(many.slice(0, 12)).length === 12 && !/more/.test(L(many.slice(0, 12)).join("")));
ok("it sits beside cascadeLines, which is unchanged", JSON.stringify(P.cascadeLines({ screens: ["s"], daemons: ["d"] })) === JSON.stringify(["\u2022 screen s", "\u2022 daemon d"]));

console.log(`\n${n} passed, 0 failed`);
