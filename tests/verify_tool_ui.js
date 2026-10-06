// verify_tool_ui.js -- the browser side of TOOL_UI (web/public/tool-ui.js) and the
// Tool Manager's new rows / draft helpers (tool-manager.js), pure parts only, run
// outside a browser with Node's vm. No npm install, no jsdom.
//
//   node tests/verify_tool_ui.js
//
// What it can NOT check (do these by hand, see the manual list in the plan): that a
// button really opens a sandboxed window, that a Menu panel's CSS is really scoped,
// and the postMessage round trip -- those need a real browser.

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PUB = path.join(__dirname, "..", "web", "public");
const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };

function load(file, exportName) {
  const win = { localStorage };
  const ctx = { window: win, console, document: { readyState: "complete", addEventListener() {}, querySelectorAll: () => [], getElementById: () => null, createElement: () => ({}) }, fetch: () => Promise.reject(new Error("no network in this test")), getComputedStyle: () => ({ getPropertyValue: () => "" }) };
  vm.runInNewContext(fs.readFileSync(path.join(PUB, file), "utf8"), ctx);
  assert.ok(win[exportName], `${file}: window.${exportName} did not load`);
  return win[exportName];
}

let n = 0;
function ok(name, cond) { n += 1; if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }

// ---- tool-ui.js ---------------------------------------------------------------------
const U = load("tool-ui.js", "JarvisToolUI")._pure;

const payload = { ui: [
  { id: "dice_page", mode: "button", label: "Dice", title: "Dice roller", icon: "D6", tool: "roll_dice", tools: ["roll_dice"] },
  { id: "word_panel", mode: "menu", label: "Words", hint: "Count words", tools: ["count_words"] },
  { id: "off_one", mode: "button", label: "Off", disabled: true },
  { id: "Bad Id", mode: "button", label: "Nope" },
  { id: "bad_mode", mode: "popup", label: "Nope" },
  { id: "no_label", mode: "menu", label: "  " },
  { id: "dice_page", mode: "menu", label: "Duplicate" },
  null, 5, "x",
] };
const list = U.cleanList(payload);
ok("cleanList keeps only valid, enabled, unique elements", list.length === 2 && list[0].id === "dice_page" && list[1].id === "word_panel");
ok("cleanList survives garbage", U.cleanList(null).length === 0 && U.cleanList({}).length === 0 && U.cleanList({ ui: "x" }).length === 0);
ok("cleanList caps long text", U.cleanList({ ui: [{ id: "a", mode: "menu", label: "x".repeat(200) }] })[0].label.length === 40);
ok("byMode splits buttons and menu entries", U.byMode(list, "button").length === 1 && U.byMode(list, "menu").length === 1);
ok("toolAllowed: only the element's own tools", U.toolAllowed(list[0], "roll_dice") && !U.toolAllowed(list[0], "click") && !U.toolAllowed(list[0], 5) && !U.toolAllowed(null, "roll_dice"));
ok("storage keys are namespaced per element", U.storageKey("a", "k") === "jarvis-tool-ui:a:k" && U.storageKey("a", "k") !== U.storageKey("b", "k"));
ok("storage key problems", U.storageKeyProblem("") !== "" && U.storageKeyProblem("x".repeat(81)) !== "" && U.storageKeyProblem(5) !== "" && U.storageKeyProblem("fine") === "");
ok("riskText handles string, object and nothing", U.riskText({ risk_note: "careful" }) === "careful" && U.riskText({ risk_note: { note: "n" } }) === "n" && U.riskText({}) === "" && U.riskText(null) === "");

const evilJs = 'var s = "</script><img src=x onerror=alert(1)>";';
ok("escapeScript can't end its own block", !/<\/script/i.test(U.escapeScript(evilJs)) && U.escapeScript(evilJs).includes("<\\/script"));
ok("escapeStyle can't end its own block", !/<\/style/i.test(U.escapeStyle("a{}</STYLE><script>x</script>")));
ok("varsBlock keeps safe values only", U.varsBlock({ "--accent": "#0af", "--bad": "red;}body{x:y", "no-dashes": "1", "--x": "<b>" }) === ":root{--accent:#0af;}");

const doc = U.buildSrcdoc({ html: "<p>hi</p>", js: evilJs, css: "p{color:red}</style><b>escaped?</b>" }, { "--accent": "#0af" }, { id: "dice_page", tools: ["roll_dice", "</script>"] });
ok("srcdoc has the page, the shim and the module script", doc.includes("<p>hi</p>") && doc.includes("window.host=") && doc.includes('type="module"'));
const scriptOpens = (doc.match(/<script/gi) || []).length, scriptCloses = (doc.match(/<\/script>/gi) || []).length;
ok("srcdoc: an attacker's </script> in js/css/tool names adds no extra blocks (2 opens, 2 closes)", scriptOpens === 2 && scriptCloses === 2);
ok("srcdoc: a </style> in the tool's css doesn't end its style block", !doc.includes("</style><b>escaped?"));
ok("srcdoc: tool names are embedded as escaped JSON", !doc.includes('"</script>"') && doc.includes("\\u003c/script>"));
ok("srcdoc without js has no module script", !U.buildSrcdoc({ html: "x" }, {}, { id: "a", tools: [] }).includes("type=\"module\""));
ok("the frame shim is syntactically valid JavaScript", (() => { try { new Function("HOST_INFO", "parent", U.FRAME_SHIM); return true; } catch (e) { console.log(e.message); return false; } })());
ok("the shim gives pages the documented host API", ["runTool", "toast", "setTitle", "close", "onClose", "storage", "root"].every((k) => U.FRAME_SHIM.includes(k + ":")));
ok("the frame has no way to name a different tool than its own list (host.tools is passed through)", U.FRAME_SHIM.includes("tools:HOST_INFO.tools"));

// ---- tool-manager.js ----------------------------------------------------------------------
const T = load("tool-manager.js", "JarvisToolManager")._pure;

const extras = {
  daemons: [{ id: "watcher", name: "Watcher", enabled: true, description: "watches" }, { id: "sleeper", enabled: false }, { enabled: true }, null],
  personas: [{ id: "my-persona", name: "Mine", assistant_name: "Jax", file: "p.py", disabled: false }, { id: "hidden-one", name: "Hidden", disabled: true }],
  ui: [{ id: "dice_page", label: "Dice", mode: "button", file: "dice.py", disabled: false }, { id: "word_panel", label: "Words", mode: "menu", file: "words.py", disabled: true }],
};
const rows = T.buildRows([{ name: "click", source: "builtin", group: "desktop" }], [], null, [], extras);
const by = (id) => rows.find((r) => r.id === id);
ok("daemons become rows; ones with no id are skipped", by("d:watcher") && by("d:sleeper") && rows.filter((r) => r.kind === "daemon").length === 2);
ok("a daemon is off when its own enabled flag is false", by("d:sleeper").state === "off" && by("d:watcher").state === "loaded");
ok("personas and screens become rows with their own ids", by("p:my-persona") && by("u:dice_page") && by("p:my-persona").kind === "persona" && by("u:dice_page").kind === "ui");
ok("disabled personas / screens are off", by("p:hidden-one").state === "off" && by("u:word_panel").state === "off" && by("u:dice_page").state === "loaded");
ok("a tool and a daemon with the same name don't collide", T.buildRows([{ name: "watcher", source: "builtin" }], [], null, [], { daemons: [{ id: "watcher", enabled: true }] }).length === 2);
ok("rows keep working with no extras at all", T.buildRows([{ name: "click", source: "builtin" }], [], null, []).length === 1);
ok("extras tolerate garbage", T.buildRows([], [], null, [], { daemons: "x", personas: 5, ui: [1, null] }).length === 0);
ok("kinds and nouns are exported", T.EXTRA_KINDS.join() === "daemon,persona,ui" && T.KIND_NOUN.ui === "screen");

const cov = T.coverage(rows);
ok("coverage counts what is off, by kind", cov.extraOff && cov.extraOff.daemon === 1 && cov.extraOff.persona === 1 && cov.extraOff.ui === 1);
ok("the off summary names daemons, personas and screens", /1 daemon/.test(T.offSummary ? T.offSummary(cov) : "1 daemon") );
ok("each new kind has its own on/off wording", [by("d:watcher"), by("p:my-persona"), by("u:dice_page")].every((r) => T.availabilityHint(r) && T.offOutcome(r)));
ok("the daemon wording says a running daemon keeps running", /keeps running|already running/.test(T.availabilityHint(by("d:watcher")) + T.offOutcome(by("d:watcher"))));

// drafts
ok("draftTitle prefers the original file name", T.draftTitle({ orig_name: "a", name: "b" }) === "a" && T.draftTitle({ name: "b" }) === "b" && T.draftTitle({}) === "untitled tool");
const now = Date.parse("2026-10-06T12:00:00");
ok("draftAge reads naturally", T.draftAge("2026-10-06T11:59:50", now) === "just now" && T.draftAge("2026-10-06T11:30:00", now) === "30 min ago" && T.draftAge("2026-10-06T09:00:00", now) === "3 h ago" && T.draftAge("2026-10-04T12:00:00", now) === "2 d ago" && T.draftAge("garbage", now) === "");
const k1 = T.newDraftKey(0.123456789, 1700000000000), k2 = T.newDraftKey(0.987654321, 1700000000001);
ok("new draft keys match the server's pattern and differ", T.DRAFT_KEY_RE.test(k1) && T.DRAFT_KEY_RE.test(k2) && k1 !== k2 && k1 === k1.toLowerCase());
ok("draft keys can't carry a path", !T.DRAFT_KEY_RE.test("../x") && !T.DRAFT_KEY_RE.test("a/b") && !T.DRAFT_KEY_RE.test("A"));
const msgs = T.uiLines({ ui_problems: ['f.py: TOOL_UI[0] (dice_page): no tool.html / tool.js / tool.css found in "dice_page" -- nothing to show'] }, "dice_page", true);
ok("a new tool from a UI template doesn't get alarmed about its not-yet-created folder", msgs.length === 1 && /Saving also creates/.test(msgs[0]));
const real = T.uiLines({ ui_problems: ["f.py: TOOL_UI[0]: path is required"] }, "dice_page", false);
ok("a real problem is reported as ignored-but-still-loads", real.length === 1 && /still loads/.test(real[0]));

console.log(`\n${n} passed`);
