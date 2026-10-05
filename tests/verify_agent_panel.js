// verify_agent_panel.js -- the Focus layout's Agent panel (agent-panel.js):
// its pure reducer and tree builder, run outside a browser. No npm install.
//   node tests/verify_agent_panel.js
const fs = require("fs"), path = require("path"), vm = require("vm");

const win = { innerWidth: 1400 };
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "..", "web", "public", "agent-panel.js"), "utf8"),
  { window: win, console, setInterval, clearInterval });
const P = win.JarvisAgentPanel._pure;

let passed = 0, failed = 0;
function check(name, ok, detail) {
  if (ok) passed += 1; else { failed += 1; console.log("FAILED: " + name + (detail !== undefined ? "  " + JSON.stringify(detail) : "")); }
}
const ev = (job, e) => P.applyEvent(job, Object.assign({ job_id: "j1" }, e), 1000);

// ---- paths -------------------------------------------------------------
check("relative stays relative", P.normPath("src/a.py", "/p") === "src/a.py");
check("./ and // collapse", P.normPath("./src//a.py", "/p") === "src/a.py");
check("absolute under root is made relative", P.normPath("/p/src/a.py", "/p") === "src/a.py");
check("windows separators + case-insensitive root", P.normPath("C:\\Proj\\src\\a.py", "c:\\proj") === "src/a.py");
check("absolute outside root is refused", P.normPath("/etc/passwd", "/p") === null);
check("escape via .. is refused", P.normPath("../x", "/p") === null);
check("empty is the root", P.normPath("", "/p") === "");
check("root-prefix lookalike is not under root", P.normPath("/pp/a.py", "/p") === null);

// ---- dev_agent flow ------------------------------------------------------
let j = P.newJob({ job_id: "j1", phase: "plan", status: "start", description: "x", project_dir: "/p" }, 0, 7);
check("dev job kind", j.kind === "dev");
ev(j, { phase: "plan", status: "start", description: "a cat app", project_dir: "/p" });
check("root from project_dir", j.root === "/p" && j.task === "a cat app");
ev(j, { phase: "plan", status: "ok", files: ["app.py", "lib/util.py"], dependencies: [], run_command: "python app.py" });
check("planned files + parent dir", j.files["app.py"].status === "planned" && j.dirs["lib"] === true);
ev(j, { phase: "write", status: "start", path: "app.py" });
check("writing is current + busy", j.files["app.py"].status === "writing" && j.current.path === "app.py");
ev(j, { phase: "write", status: "ok", path: "app.py", bytes: 120, preview: "SECRET" });
check("written with bytes", j.files["app.py"].status === "written" && j.files["app.py"].bytes === 120);
check("preview text is never stored", JSON.stringify(j).indexOf("SECRET") === -1);
ev(j, { phase: "run", status: "fail", exit_code: 1, stderr_tail: "boom\nmore" });
check("failed run logged, first line only", j.log.some((l) => l.level === "fail" && /boom/.test(l.text) && !/more/.test(l.text)));
ev(j, { phase: "fix", status: "start", attempt: 1, max_attempts: 3, classified_error: "syntax", target_file: "app.py" });
check("fixing marks target", j.files["app.py"].status === "fixing");
ev(j, { phase: "fix", status: "ok", attempt: 1, max_attempts: 3, target_file: "app.py" });
check("fixed", j.files["app.py"].status === "fixed");
ev(j, { phase: "done", status: "ok", project_dir: "/p" });
check("done ok", j.status === "ok" && j.current === null && j.endedAt === 1000);

// ---- code_agent flow ------------------------------------------------------
let c = P.newJob({ job_id: "c1", phase: "plan", status: "start", task: "t", root: "/r" }, 0, null);
check("code job kind", c.kind === "code");
ev(c, { phase: "plan", status: "start", task: "fix the bug", root: "/r" });
ev(c, { phase: "step", status: "start", tool: "list_dir", arguments: { path: "." } });
ev(c, { phase: "step", status: "ok", tool: "list_dir", outcome: "4 entries",
        listing: ["src/", "  a.py", "  deep/", "    b.py", "README.md"] });
check("listing builds nested tree", c.files["src/a.py"].status === "listed" && c.files["src/deep/b.py"].status === "listed" && c.files["README.md"] && c.dirs["src/deep"]);
ev(c, { phase: "step", status: "start", tool: "list_dir", arguments: { path: "src" } });
ev(c, { phase: "step", status: "ok", tool: "list_dir", listing: ["c.py"] });
check("sub-listing is relative to the listed dir", !!c.files["src/c.py"]);
ev(c, { phase: "step", status: "start", tool: "read_file", arguments: { path: "src/a.py" } });
check("read in flight is current", c.current.path === "src/a.py");
ev(c, { phase: "step", status: "ok", tool: "read_file", outcome: "31 lines" });
check("read", c.files["src/a.py"].status === "read");
ev(c, { phase: "step", status: "start", tool: "edit_file", arguments: { path: "/r/src/a.py" } });
ev(c, { phase: "step", status: "ok", tool: "edit_file", outcome: "edited" });
check("absolute path inside root is the same file", c.files["src/a.py"].status === "edited" && Object.keys(c.files).every((k) => !k.startsWith("/")));
ev(c, { phase: "step", status: "start", tool: "read_file", arguments: { path: "src/a.py" } });
ev(c, { phase: "step", status: "ok", tool: "read_file", outcome: "31 lines" });
check("re-reading an edited file does not demote it", c.files["src/a.py"].status === "edited");
ev(c, { phase: "step", status: "start", tool: "write_file", arguments: { path: "new.py" } });
ev(c, { phase: "step", status: "fail", tool: "write_file", error: "already exists\nx" });
check("failed write", c.files["new.py"].status === "failed" && c.log.some((l) => /already exists/.test(l.text)));
ev(c, { phase: "step", status: "start", tool: "run_shell", arguments: { command: "pytest" } });
ev(c, { phase: "step", status: "ok", tool: "run_shell", outcome: "exit 1: boom" });
check("non-zero shell is a fail line", c.log[c.log.length - 1].level === "fail");
ev(c, { phase: "step", status: "start", tool: "read_file", arguments: { path: "/etc/passwd" } });
ev(c, { phase: "step", status: "ok", tool: "read_file", outcome: "x" });
check("path outside the root is never drawn", !Object.keys(c.files).some((k) => /passwd/.test(k)));
ev(c, { phase: "done", status: "fail", error: "agent stopped", tool_calls: 9 });
check("done fail", c.status === "fail" && /agent stopped/.test(c.error));

// ---- tree ------------------------------------------------------------------
let t = P.buildRows(c, false);
check("touched-only hides listed files", !t.rows.some((r) => r.name === "README.md") && t.hidden >= 2, t.hidden);
check("dirs before files, folders with no touched file are hidden", t.rows.length > 0 && !t.rows.some((r) => r.name === "deep"));
let all = P.buildRows(c, true);
check("show-all includes listed files", all.rows.some((r) => r.name === "README.md") && all.rows.some((r) => r.name === "b.py"));
check("depth follows nesting", all.rows.find((r) => r.name === "b.py").depth === 2);
check("cap truncates and says so", P.buildRows(c, true, 2).truncated === true);
check("summary counts", P.summarize(c).edited === 1 && P.summarize(c).failed === 1);

// ---- a failure must not erase an earlier success (bug-hunt fix) -------------
let f = P.newJob({ job_id: "f1", phase: "plan", status: "start", task: "t", root: "/r" }, 0, null);
const step = (tool, args, st, extra) => P.applyEvent(f, Object.assign({ job_id: "f1", phase: "step", status: st, tool, arguments: args }, extra), 1);
step("edit_file", { path: "a.py" }, "start"); step("edit_file", { path: "a.py" }, "ok", { outcome: "edited" });
check("first edit lands", f.files["a.py"].status === "edited");
step("edit_file", { path: "a.py" }, "start");
check("a second edit in flight shows as writing", f.files["a.py"].status === "writing");
step("edit_file", { path: "a.py" }, "fail", { error: "old_str not found" });
check("a failed second edit keeps the file 'edited'", f.files["a.py"].status === "edited", f.files["a.py"]);
check("...and the failure is still in the log", f.log.some((l) => l.level === "fail" && /old_str not found/.test(l.text)));
check("...with no stale failure note on the row", !f.files["a.py"].note);
step("edit_file", { path: "a.py" }, "start"); step("edit_file", { path: "a.py" }, "ok", { outcome: "edited" });
check("a later success on it is still 'edited'", f.files["a.py"].status === "edited");
step("write_file", { path: "n.py" }, "start"); step("write_file", { path: "n.py" }, "fail", { error: "already exists" });
check("a failure on a file never changed is 'failed'", f.files["n.py"].status === "failed" && /already exists/.test(f.files["n.py"].note));
step("edit_file", { path: "n.py" }, "start"); step("edit_file", { path: "n.py" }, "ok", { outcome: "edited" });
check("a failed file that is later edited recovers", f.files["n.py"].status === "edited");
let g = P.newJob({ job_id: "g1", phase: "plan", status: "start", description: "d" }, 0, null);
const dv = (e) => P.applyEvent(g, Object.assign({ job_id: "g1" }, e), 1);
dv({ phase: "plan", status: "start", description: "d", project_dir: "/p" });
dv({ phase: "write", status: "start", path: "app.py" }); dv({ phase: "write", status: "ok", path: "app.py", bytes: 5 });
dv({ phase: "fix", status: "start", attempt: 1, max_attempts: 3, target_file: "app.py" });
dv({ phase: "fix", status: "fail", attempt: 1, max_attempts: 3, target_file: "app.py", note: "still broken" });
check("a failed FIX still shows 'failed' (the file is still broken)", g.files["app.py"].status === "failed" && /still broken/.test(g.files["app.py"].note));

check("elapsed format", P.fmtElapsed(65000) === "1m 05s" && P.fmtElapsed(4000) === "4s" && P.fmtElapsed(-1) === "");
check("bytes format", P.fmtBytes(500) === "500 B" && P.fmtBytes(2048) === "2.0 KB");

console.log(`${passed} passed${failed ? ", " + failed + " FAILED" : ""}`);
process.exit(failed ? 1 : 0);
