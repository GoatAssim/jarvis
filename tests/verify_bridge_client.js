// web/bridge.js -- the server's side of the direct channel to jarvis -- against a FAKE
// worker that speaks the same line-delimited JSON (see jarvis-cli/jarvis/bridge.py for
// the wire). Pure pool logic: no Python, no jarvis, no npm install needed.
//
//   node tests/verify_bridge_client.js
//
// Pinned here:
//   - request() never blocks and never queues: no idle worker -> null (the server then
//     starts a process itself), and a second worker is warmed for next time
//   - a worker is reused; a confirmation round-trips through handle.write()
//   - `refuse` / `retire` / a death before `started` come back as { fallback: true } so
//     the caller can run the request another way, and the stale worker is replaced
//   - a death AFTER `started` is reported as a crash, never re-run (no double side effects)
//   - kill() reports { killed: true }; a worker that never gets ready trips the breaker
//     after three tries and every request falls back; enabled:false never spawns
//   - a worker is replaced after maxRequests, and a request handed out in the same tick
//     that retires it is not cut off

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const { createBridge, bridgeSettingsFromEnv } = await import(
  pathToFileURL(path.join(here, "..", "web", "bridge.js")).href
);

let passed = 0;
let failed = 0;
function check(name, cond, detail = "") {
  if (cond) { passed += 1; console.log("ok  " + name); }
  else { failed += 1; console.log("FAIL " + name + (detail ? `: ${JSON.stringify(detail)}` : "")); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 4000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (fn()) return true; await sleep(15); }
  return !!fn();
}

// ---- the fake worker ---------------------------------------------------------------------
const fakePath = path.join(os.tmpdir(), `jarvis-fake-bridge-${process.pid}.js`);
fs.writeFileSync(fakePath, `
const mode = process.env.FAKE_MODE || "ok";
const send = (o) => process.stdout.write(JSON.stringify(o) + "\\n");
if (mode === "crash-before-ready") process.exit(3);
if (mode !== "never-ready") send({ t: "ready", proto: 1, pid: process.pid, boot_ms: 1 });
let waiting = null;
let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf("\\n")) !== -1) {
    const line = buf.slice(0, i); buf = buf.slice(i + 1);
    let m; try { m = JSON.parse(line); } catch { continue; }
    if (m.op === "ping") send({ t: "pong" });
    else if (m.op === "shutdown") process.exit(0);
    else if (m.op === "answer" && waiting && waiting.id === m.id) {
      send({ id: m.id, t: "out", line: "answered:" + m.line });
      send({ id: m.id, t: "exit", code: 0, ms: 1 });
      waiting = null;
    } else if (m.op === "run") {
      const id = m.id, a = m.argv;
      if (mode === "retire") { send({ id, t: "retire", why: "stale" }); return; }
      if (a[0] === "refuseme") { send({ id, t: "refuse", why: "needs a process" }); continue; }
      if (mode === "die-before-start") process.exit(9);
      send({ id, t: "started", pid: process.pid });
      if (a[0] === "die-mid") process.exit(7);
      if (a[0] === "hang") continue;
      if (a[0] === "confirm") { send({ id, t: "confirm", tool: "x", arguments: {}, raw: "JARVIS_CONFIRM_REQUEST {}" }); waiting = { id }; continue; }
      send({ id, t: "err", line: "trace", cat: "tool-call" });
      send({ id, t: "out", line: "argv=" + a.join(" ") + " env=" + (m.env.K || "") });
      send({ id, t: "exit", code: a[0] === "fail" ? 2 : 0, ms: 1 });
    }
  }
});
`);

function make(mode = "ok", extra = {}) {
  const logs = [];
  const bridge = createBridge({
    getInvoker: () => ({ cmd: process.execPath, args: [fakePath] }),
    env: () => ({ FAKE_MODE: mode }),
    log: (m) => logs.push(m),
    ...extra,
  });
  return { bridge, logs };
}

function ask(bridge, argv, over = {}) {
  const r = { events: [], finish: null };
  r.handle = bridge.request({
    argv, env: over.env || {}, kind: "ask", interactive: true,
    onEvent: (ev) => r.events.push(ev),
    onFinish: (info) => { r.finish = info; },
  });
  return r;
}
const ready = (bridge) => until(() => bridge.status().workers.some((w) => w.state === "idle"));

// ---- tests -------------------------------------------------------------------------------
async function main() {
  // 1. never blocks / never queues; a warm worker then serves and is reused
  {
    const { bridge } = make();
    check("nothing is idle before warm-up, so request() says no (the caller spawns a process)",
      ask(bridge, ["a"]).handle === null);
    check("...and asking warmed a worker for next time", await ready(bridge));
    const r1 = ask(bridge, ["hello", "world"], { env: { K: "v" } });
    check("a request is accepted once a worker is idle", !!r1.handle);
    await until(() => r1.finish);
    check("events arrive typed and in order", r1.events.map((e) => e.t).join() === "err,out", r1.events);
    check("a category rides along on a stderr event", r1.events[0].cat === "tool-call");
    check("argv and the env overlay reached the worker", r1.events[1].line === "argv=hello world env=v", r1.events[1]);
    check("finished with the exit code", r1.finish.code === 0 && !r1.finish.fallback, r1.finish);
    const pid1 = r1.handle.pid;
    const r2 = ask(bridge, ["again"]);
    await until(() => r2.finish);
    check("the same worker served the next request", r2.handle.pid === pid1);
    const r3 = ask(bridge, ["fail"]);
    await until(() => r3.finish);
    check("a non-zero exit is reported as such, not as a fallback", r3.finish.code === 2 && !r3.finish.fallback);
    bridge.shutdown();
  }

  // 2. busy worker -> null (no queue), and a second worker is warmed
  {
    const { bridge } = make();
    bridge.warm();
    await ready(bridge);
    const busy = ask(bridge, ["hang"]);
    check("long request accepted", !!busy.handle);
    const second = ask(bridge, ["x"]);
    check("while it is busy, another request is declined rather than queued", second.handle === null);
    check("and a second worker is started for the next one",
      await until(() => bridge.status().workers.filter((w) => w.state === "idle").length === 1));
    const third = ask(bridge, ["y"]);
    check("the new worker takes it", !!third.handle && third.handle.pid !== busy.handle.pid);
    await until(() => third.finish);
    busy.handle.kill();
    await until(() => busy.finish);
    check("kill() ends the request as `killed`", busy.finish && busy.finish.killed === true, busy.finish);
    bridge.shutdown();
  }

  // 3. confirmation round trip
  {
    const { bridge } = make();
    bridge.warm();
    await ready(bridge);
    const r = ask(bridge, ["confirm"]);
    await until(() => r.events.some((e) => e.t === "confirm"));
    check("a typed confirm event arrives", r.events.some((e) => e.t === "confirm" && e.tool === "x"));
    check("the answer line is delivered", r.handle.write("y") === true);
    await until(() => r.finish);
    check("and the request completes from it", r.events.some((e) => e.line === "answered:y") && r.finish.code === 0, r.events);
    bridge.shutdown();
  }

  // 4. refuse -> fallback, worker stays usable
  {
    const { bridge } = make();
    bridge.warm();
    await ready(bridge);
    const r = ask(bridge, ["refuseme"]);
    await until(() => r.finish);
    check("refuse is a fallback, nothing ran", r.finish.fallback === true && r.events.length === 0, r.finish);
    const ok = ask(bridge, ["fine"]);
    check("the worker is still idle and takes the next request", !!ok.handle);
    await until(() => ok.finish);
    bridge.shutdown();
  }

  // 5. retire (stale) -> fallback + replacement
  {
    const { bridge } = make("retire");
    bridge.warm();
    await ready(bridge);
    const old = bridge.status().workers[0].pid;
    const r = ask(bridge, ["x"]);
    await until(() => r.finish);
    check("a stale worker hands the request back before running it", r.finish.fallback === true && r.finish.retired === true, r.finish);
    check("and is replaced by a fresh one",
      await until(() => bridge.status().workers.some((w) => w.state === "idle" && w.pid !== old)));
    bridge.shutdown();
  }

  // 6. death before started = fallback; death after started = crash (never re-run)
  {
    const { bridge } = make("die-before-start");
    bridge.warm();
    await ready(bridge);
    const r = ask(bridge, ["x"]);
    await until(() => r.finish);
    check("dying before the request began is a fallback", r.finish.fallback === true, r.finish);
    bridge.shutdown();
  }
  {
    const { bridge } = make();
    bridge.warm();
    await ready(bridge);
    const r = ask(bridge, ["die-mid"]);
    await until(() => r.finish);
    check("dying after it began is a crash, NOT a fallback (it must not run twice)",
      r.finish.crashed === true && !r.finish.fallback && r.finish.code === 7, r.finish);
    check("and the pool recovers",
      await until(() => bridge.status().workers.some((w) => w.state === "idle")));
    bridge.shutdown();
  }

  // 7. a worker that never gets ready trips the breaker; disabled never spawns
  {
    const { bridge, logs } = make("never-ready", { readyMs: 80 });
    bridge.warm();
    await until(() => bridge.status().breaker, 6000);
    check("three failed warm-ups open the breaker", bridge.status().breaker === true, bridge.status());
    check("it said so in the log", logs.some((l) => /giving up/.test(l)), logs);
    check("every request now declines", ask(bridge, ["x"]).handle === null);
    bridge.shutdown();
  }
  {
    const { bridge } = make("crash-before-ready");
    bridge.warm();
    await until(() => bridge.status().breaker, 6000);
    check("a worker that crashes at start trips it too", bridge.status().breaker === true);
    bridge.shutdown();
  }
  {
    const { bridge } = make("ok", { enabled: false });
    bridge.warm();
    check("enabled:false never starts anything", bridge.status().workers.length === 0 && ask(bridge, ["x"]).handle === null);
    bridge.shutdown();
  }

  // 8. recycling after maxRequests, without cutting off a request handed out in the same tick
  {
    const { bridge } = make("ok", { maxRequests: 2 });
    bridge.warm();
    await ready(bridge);
    const first = bridge.status().workers[0].pid;
    const a = ask(bridge, ["1"]);
    await until(() => a.finish);
    let b;
    b = bridge.request({
      argv: ["2"], env: {}, kind: "ask", interactive: true, onEvent: () => {},
      onFinish: (info) => {
        // Chained inside the callback of the request that hits the limit.
        b = { info };
      },
    });
    await until(() => b && b.info);
    check("the second request completed even though it was the last the worker may serve", b.info.code === 0, b.info);
    check("then a fresh worker replaced it",
      await until(() => bridge.status().workers.some((w) => w.state === "idle" && w.pid !== first)));
    bridge.shutdown();
  }
  {
    const { bridge } = make("ok", { maxRequests: 1 });
    bridge.warm();
    await ready(bridge);
    let chained = "not called";
    bridge.request({
      argv: ["1"], env: {}, kind: "ask", interactive: true, onEvent: () => {},
      onFinish: () => { chained = bridge.request({ argv: ["2"], env: {}, kind: "ask", onEvent: () => {}, onFinish: () => {} }); },
    });
    await until(() => chained !== "not called");
    check("a worker at its limit is not handed another request from inside the finish callback", chained === null, chained);
    bridge.shutdown();
  }

  // 9. settings
  {
    check("JARVIS_BRIDGE=0 disables", bridgeSettingsFromEnv({ JARVIS_BRIDGE: "0" }).enabled === false);
    check("on by default", bridgeSettingsFromEnv({}).enabled === true);
    check("max is clamped to 1..8 with a default", bridgeSettingsFromEnv({ JARVIS_BRIDGE_MAX: "99" }).max === 8
      && bridgeSettingsFromEnv({ JARVIS_BRIDGE_MAX: "x" }).max === 3);
  }

  try { fs.unlinkSync(fakePath); } catch { /* ignore */ }
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main();
