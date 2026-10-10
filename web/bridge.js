// jarvis-web <-> jarvis, the direct channel.
//
// The server used to start a new `jarvis` process for every request (an ask, a
// panel refresh, the scheduler's tick) and take its two text pipes apart again.
// This keeps a few warm `jarvis _internal_bridge` workers (jarvis-cli/jarvis/
// bridge.py, which documents the wire) and hands them requests over a typed,
// line-delimited JSON channel.
//
// What this is NOT: a replacement. server.js still owns the original
// spawn-per-request code and falls back to it whenever request() says "no" or a
// request finishes with { fallback: true }. A worker that will not start, is
// busy, is stale, refuses a command, or dies before running anything costs a
// request nothing but the old behaviour. Nothing here is needed for jarvis to
// work; jarvis knows nothing about this file.
//
// One request at a time per worker (the process environment and stdio are
// process-global there). Up to `max` workers; request() never queues -- with
// none idle it returns null and warms another worker for next time.

import { spawn } from "node:child_process";

export function killProcessTree(child) {
  if (!child || child.pid == null) return;
  if (process.platform === "win32") {
    try {
      spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
    } catch { /* ignore */ }
  } else {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      try { child.kill("SIGTERM"); } catch { /* ignore */ }
    }
  }
}

function clampInt(value, lo, hi, fallback) {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
}

// Settings come from the server's environment; see the options below for what each means.
export function bridgeSettingsFromEnv(env = process.env) {
  return {
    enabled: env.JARVIS_BRIDGE !== "0",
    max: clampInt(env.JARVIS_BRIDGE_MAX, 1, 8, 3),
    maxRequests: clampInt(env.JARVIS_BRIDGE_MAX_REQUESTS, 5, 100000, 150),
  };
}

/**
 * options:
 *   getInvoker()   -> { cmd, args } | null   how to start jarvis (server.js's JARVIS)
 *   env()          -> object                 extra environment for a worker
 *   enabled        -> false turns the whole thing off (JARVIS_BRIDGE=0)
 *   max            -> most workers at once
 *   maxRequests    -> a worker is replaced after serving this many
 *   idleMs         -> the last worker is shut down after this long unused
 *   extraIdleMs    -> workers beyond the first, after this long unused
 *   readyMs        -> how long a worker may take to warm up
 *   subcommand     -> the hidden jarvis subcommand to start (tests change it)
 *   log(msg)       -> diagnostics
 */
export function createBridge(options = {}) {
  const opts = {
    enabled: true, max: 3, maxRequests: 150,
    idleMs: 15 * 60 * 1000, extraIdleMs: 90 * 1000, readyMs: 90 * 1000,
    subcommand: "_internal_bridge", env: () => ({}), log: () => {},
    ...options,
  };
  const workers = [];
  let seq = 0;
  let failures = 0;       // consecutive workers that never reached "ready"
  let breaker = false;    // too many failures: stop trying until the server restarts
  let stopped = false;

  const send = (w, obj) => {
    try { w.proc.stdin.write(JSON.stringify(obj) + "\n"); return true; } catch { return false; }
  };

  function removeWorker(w) {
    const i = workers.indexOf(w);
    if (i !== -1) workers.splice(i, 1);
  }

  function finishRequest(w, info) {
    const req = w.req;
    w.req = null;
    if (!req) return;
    try { req.onFinish(info); } catch (e) { opts.log(`bridge: onFinish threw: ${e.message}`); }
  }

  function handleEvent(w, ev) {
    if (!ev || typeof ev !== "object") return;
    if (ev.t === "ready") {
      if (w.state === "starting") {
        clearTimeout(w.readyTimer);
        w.state = "idle";
        w.lastUsed = Date.now();
        failures = 0;
        opts.log(`bridge: worker ${w.proc.pid} ready (${ev.boot_ms ?? "?"} ms)`);
      }
      return;
    }
    if (ev.t === "fatal") {
      w.fatal = String(ev.error || "").slice(0, 400);
      return;
    }
    const req = w.req;
    if (!req || ev.id !== req.id) return;   // pong, or a straggler from a request already finished
    switch (ev.t) {
      case "started":
        req.started = true;
        break;
      case "exit":
        w.served += 1;
        w.lastUsed = Date.now();
        // Decided BEFORE the callback runs: it may hand this worker its next request.
        w.state = w.served >= opts.maxRequests ? "stopping" : "idle";
        finishRequest(w, { code: typeof ev.code === "number" ? ev.code : 1, signal: null, ms: ev.ms });
        if (w.state === "stopping") retireWorker(w, "recycled");
        break;
      case "refuse":
        w.state = "idle";
        finishRequest(w, { fallback: true, reason: String(ev.why || "refused") });
        break;
      case "retire":
        // Stale: nothing ran. This worker is done; the caller runs the request
        // another way, and a fresh worker is warmed for the next one.
        w.state = "dead";
        finishRequest(w, { fallback: true, reason: String(ev.why || "stale"), retired: true });
        killProcessTree(w.proc);
        removeWorker(w);
        ensureWarm();
        break;
      default:
        try { req.onEvent(ev); } catch (e) { opts.log(`bridge: onEvent threw: ${e.message}`); }
    }
  }

  function startWorker() {
    if (stopped || breaker || !opts.enabled || workers.length >= opts.max) return null;
    const invoker = opts.getInvoker();
    if (!invoker) return null;
    let proc;
    try {
      proc = spawn(invoker.cmd, [...invoker.args, opts.subcommand], {
        windowsHide: true,
        detached: process.platform !== "win32",
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8", ...opts.env() },
      });
    } catch (e) {
      noteFailure(`couldn't start a worker: ${e.message}`);
      return null;
    }
    const w = { proc, state: "starting", req: null, served: 0, lastUsed: Date.now(),
                killedByUs: false, stderrTail: "", fatal: "", readyTimer: null };
    workers.push(w);
    w.readyTimer = setTimeout(() => {
      if (w.state === "starting") {
        w.note = `did not become ready in ${opts.readyMs} ms`;
        killProcessTree(proc);   // the exit handler below counts it
      }
    }, opts.readyMs);
    w.readyTimer.unref?.();

    let buf = "";
    proc.stdout.setEncoding("utf8");
    proc.stdout.on("data", (chunk) => {
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        handleEvent(w, ev);
      }
    });
    proc.stderr.setEncoding("utf8");
    proc.stderr.on("data", (d) => { w.stderrTail = (w.stderrTail + d).slice(-4000); });
    proc.stdin.on("error", () => { /* the exit handler reports a dead worker */ });
    // A worker that never got ready is one failure, however it went wrong (could not be
    // spawned, crashed while warming up, or timed out): counted once, then retried until
    // the breaker opens.
    const failedStart = (why) => {
      if (w.failedStart) return;
      w.failedStart = true;
      clearTimeout(w.readyTimer);
      w.state = "dead";
      removeWorker(w);
      noteFailure(`worker ${proc.pid ?? "?"} ${why}`);
      if (!stopped && !breaker) {
        const retry = setTimeout(ensureWarm, 150 * failures);
        retry.unref?.();
      }
    };
    proc.on("error", (e) => {
      if (w.state === "starting") failedStart(`failed to start: ${e.message}`);
    });
    proc.on("exit", (code, signal) => {
      clearTimeout(w.readyTimer);
      const wasStarting = w.state === "starting";
      const was = w.req;
      if (wasStarting) {
        const detail = (w.fatal || w.stderrTail).trim().split("\n").pop();
        failedStart((w.note || `exited before it was ready (code ${code})`) + (detail ? `: ${detail}` : ""));
      }
      w.state = "dead";
      removeWorker(w);
      if (was) {
        if (w.killedByUs) finishRequest(w, { code, signal, killed: true });
        else if (!was.started) finishRequest(w, { fallback: true, reason: "worker exited before running the request" });
        else finishRequest(w, { code: code ?? 1, signal, crashed: true });
      }
      if (!stopped && !wasStarting) ensureWarm();
    });
    return w;
  }

  function noteFailure(msg) {
    failures += 1;
    opts.log(`bridge: ${msg}`);
    if (failures >= 3 && !breaker) {
      breaker = true;
      opts.log("bridge: giving up on warm workers for this server session; every request will start its own jarvis process (as before)");
    }
  }

  function ensureWarm() {
    if (stopped || breaker || !opts.enabled) return;
    if (workers.some((w) => w.state === "starting" || w.state === "idle")) return;
    startWorker();
  }

  function retireWorker(w, why) {
    if (w.state === "dead" || w.retiring) return;
    w.retiring = true;
    w.state = "stopping";
    opts.log(`bridge: worker ${w.proc.pid} retired (${why})`);
    send(w, { op: "shutdown" });
    const t = setTimeout(() => killProcessTree(w.proc), 2000);
    t.unref?.();
    ensureWarm();
  }

  const reaper = setInterval(() => {
    const now = Date.now();
    const idle = workers.filter((w) => w.state === "idle");
    for (const w of idle) {
      const limit = workers.length > 1 ? opts.extraIdleMs : opts.idleMs;
      if (now - w.lastUsed > limit) retireWorker(w, "idle");
    }
  }, 30 * 1000);
  reaper.unref?.();

  return {
    // Warm one worker now (server startup). Never throws, never waits.
    warm() { ensureWarm(); },

    // req: { argv, env, stdin, interactive, kind, onEvent(ev), onFinish(info) }
    // Returns a handle { id, pid, write(line), kill() }, or null when the caller
    // should start a process itself (nothing is sent in that case).
    request(req) {
      if (stopped || breaker || !opts.enabled) return null;
      let w = null;
      for (const c of workers) {
        if (c.state === "idle" && (!w || c.lastUsed > w.lastUsed)) w = c;
      }
      if (!w) {
        ensureWarm();
        if (workers.length < opts.max && !workers.some((c) => c.state === "starting")) startWorker();
        return null;
      }
      const id = `r${++seq}`;
      const msg = {
        op: "run", id, argv: req.argv, env: req.env || {},
        stdin: typeof req.stdin === "string" ? req.stdin : null,
        interactive: !!req.interactive, kind: req.kind || "once",
      };
      w.state = "busy";
      w.req = { id, started: false, onEvent: req.onEvent || (() => {}), onFinish: req.onFinish || (() => {}) };
      if (!send(w, msg)) {
        w.req = null;
        w.state = "dead";
        killProcessTree(w.proc);
        removeWorker(w);
        return null;
      }
      return {
        id,
        pid: w.proc.pid,
        write(line) { return send(w, { op: "answer", id, line: String(line ?? "") }); },
        kill() { w.killedByUs = true; killProcessTree(w.proc); },
      };
    },

    status() {
      return {
        enabled: opts.enabled, breaker, max: opts.max, maxRequests: opts.maxRequests,
        workers: workers.map((w) => ({ pid: w.proc.pid, state: w.state, served: w.served, busy: !!w.req })),
      };
    },

    shutdown() {
      stopped = true;
      clearInterval(reaper);
      for (const w of workers.slice()) killProcessTree(w.proc);
    },
  };
}
