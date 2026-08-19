// jarvis-web server
//
// Talks to the REAL jarvis CLI in two ways:
//   1. Reads/writes the actual ~/.jarvis/commands.json directly for listing,
//      creating, editing, and deleting commands (structured data).
//   2. Spawns the actual `jarvis` process to execute commands (single or
//      chained with "then"), streaming its stdout/stderr live over a
//      WebSocket. Execution logic is never reimplemented here — whatever
//      the CLI does is exactly what runs.
//
// Binds to 127.0.0.1 only. This tool runs real shell commands from your
// commands.json, on your machine, exactly like the CLI does — it isn't
// meant to be exposed beyond localhost.

import express from "express";
import { WebSocketServer } from "ws";
import { spawn } from "node:child_process";
import { watch } from "node:fs";
import { createServer } from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT) || 4173;
const HOST = "127.0.0.1";

const RESERVED_NAMES = new Set(["config", "ai-config", "ai-clear", "ai-drop-from", "playnite-config", "spotify-config", "spotify-login", "memory-config", "then", "and", "-h", "--help"]);

// ---------------------------------------------------------------------------
// Locate the real jarvis binary. Tries a few invocation strategies, in
// order, and caches whichever one actually works.
// ---------------------------------------------------------------------------

const CANDIDATES = process.env.JARVIS_BIN
  ? [parseOverride(process.env.JARVIS_BIN)]
  : [
      { cmd: "jarvis", args: [] },
      { cmd: "python3", args: ["-m", "jarvis"] },
      { cmd: "python", args: ["-m", "jarvis"] },
      { cmd: "py", args: ["-m", "jarvis"] },
    ];

function parseOverride(raw) {
  const parts = raw.split(" ").filter(Boolean);
  return { cmd: parts[0], args: parts.slice(1) };
}

function tryInvoker({ cmd, args }, timeoutMs = 4000) {
  return new Promise((resolve) => {
    let settled = false;
    let child;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        child.kill();
      } catch {
        /* ignore */
      }
      resolve(null);
    }, timeoutMs);

    try {
      child = spawn(cmd, [...args, "config"], { windowsHide: true });
    } catch {
      clearTimeout(timer);
      resolve(null);
      return;
    }

    let out = "";
    child.stdout?.on("data", (d) => (out += d.toString()));
    child.on("error", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(null);
    });
    child.on("exit", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const configPath = out.trim();
      if (code === 0 && configPath) {
        resolve({ cmd, args, configPath });
      } else {
        resolve(null);
      }
    });
  });
}

let JARVIS = null; // { cmd, args, configPath }
let AI_CONFIG_PATH = null; // cached path to ai_config.json, discovered lazily via `jarvis ai-config`
let PLAYNITE_CONFIG_PATH = null; // cached path to playnite.json via `jarvis playnite-config`
let SPOTIFY_CONFIG_PATH = null;
let MEMORY_CONFIG_PATH = null;

async function resolveJarvis() {
  for (const candidate of CANDIDATES) {
    const result = await tryInvoker(candidate);
    if (result) return result;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Config file helpers — read/write the real commands.json directly.
// ---------------------------------------------------------------------------

async function readConfig() {
  const text = await fs.readFile(JARVIS.configPath, "utf-8");
  const data = JSON.parse(text);
  if (!data.commands || typeof data.commands !== "object") {
    data.commands = {};
  }
  return data;
}

async function writeConfig(data) {
  const text = JSON.stringify(data, null, 2) + "\n";
  suppressWatchUntil = Date.now() + 400;
  await fs.writeFile(JARVIS.configPath, text, "utf-8");
  broadcastCommands(data.commands);
}

// ---------------------------------------------------------------------------
// Live commands sync — push commands.json changes to every open browser tab.
// ---------------------------------------------------------------------------

let suppressWatchUntil = 0;
let configWatchTimer = null;
let configWatchStop = null;
let broadcastCommands = () => {};

function stopConfigWatcher() {
  if (configWatchStop) {
    configWatchStop();
    configWatchStop = null;
  }
  clearTimeout(configWatchTimer);
}

function startConfigWatcher() {
  stopConfigWatcher();
  if (!JARVIS?.configPath) return;
  try {
    configWatchStop = watch(JARVIS.configPath, { persistent: false }, () => {
      if (Date.now() < suppressWatchUntil) return;
      clearTimeout(configWatchTimer);
      configWatchTimer = setTimeout(async () => {
        try {
          const data = await readConfig();
          broadcastCommands(data.commands);
        } catch {
          /* mid-write or transient read error */
        }
      }, 200);
    });
  } catch (e) {
    console.warn(`Couldn't watch commands.json: ${e.message}`);
  }
}

function validateName(name, { forbidExisting, existing } = {}) {
  if (typeof name !== "string" || !name.trim()) {
    return "Command name can't be empty.";
  }
  if (/\s/.test(name)) {
    return "Command name can't contain spaces.";
  }
  if (/[\r\n\0]/.test(name)) {
    return "Command name contains invalid characters.";
  }
  if (RESERVED_NAMES.has(name)) {
    return `"${name}" is reserved by jarvis (${[...RESERVED_NAMES].sort().join(", ")}) and can't be used as a command name.`;
  }
  if (forbidExisting && existing && Object.prototype.hasOwnProperty.call(existing, name)) {
    return `A command named "${name}" already exists.`;
  }
  return null;
}

function validateSpec(spec) {
  if (typeof spec !== "object" || spec === null || Array.isArray(spec)) {
    return "Command spec must be an object.";
  }
  if (spec.run === undefined || spec.run === null) {
    return "Command must have a 'run' (string or list of steps).";
  }
  const steps = Array.isArray(spec.run) ? spec.run : [spec.run];
  if (steps.length === 0) {
    return "Command's 'run' list can't be empty.";
  }
  for (const [i, step] of steps.entries()) {
    if (typeof step === "string") continue;
    if (typeof step === "object" && step !== null && typeof step.run === "string") {
      if (step.if !== undefined && typeof step.if !== "string" && typeof step.if !== "object") {
        return `Step ${i + 1}: 'if' must be a string or object.`;
      }
      if (step.unless !== undefined && typeof step.unless !== "string" && typeof step.unless !== "object") {
        return `Step ${i + 1}: 'unless' must be a string or object.`;
      }
      if (step.parallel !== undefined && typeof step.parallel !== "boolean") {
        return `Step ${i + 1}: 'parallel' must be true or false.`;
      }
      if (step.showCommand !== undefined && typeof step.showCommand !== "boolean") {
        return `Step ${i + 1}: 'showCommand' must be true or false.`;
      }
      if (step.if && typeof step.if === "object") {
        const varNames = Object.keys(spec.vars || {});
        for (const key of Object.keys(step.if)) {
          if (!varNames.includes(key)) {
            return `Step ${i + 1}: condition uses unknown variable "${key}" (this command's variables are: ${varNames.join(", ") || "(none)"}).`;
          }
        }
      }
      continue;
    }
    return `Step ${i + 1} must be a string or an object with a 'run' field.`;
  }
  if (spec.vars !== undefined && (typeof spec.vars !== "object" || Array.isArray(spec.vars))) {
    return "'vars' must be an object.";
  }
  return null;
}

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json({ limit: "1mb" }));
app.use(express.static(path.join(__dirname, "public")));

function requireJarvis(req, res, next) {
  if (!JARVIS) {
    return res.status(503).json({
      error:
        "Can't find the jarvis CLI. Make sure it's installed (pip install .) and on PATH, or set JARVIS_BIN.",
    });
  }
  next();
}

app.get("/api/status", async (req, res) => {
  res.json({
    online: !!JARVIS,
    invocation: JARVIS ? [JARVIS.cmd, ...JARVIS.args].join(" ") : null,
    configPath: JARVIS ? JARVIS.configPath : null,
  });
});

app.post("/api/reconnect", async (req, res) => {
  JARVIS = await resolveJarvis();
  AI_CONFIG_PATH = null; // re-discover on next config request too, in case the binary changed
  PLAYNITE_CONFIG_PATH = null;
  SPOTIFY_CONFIG_PATH = null;
  MEMORY_CONFIG_PATH = null;
  startConfigWatcher();
  res.json({
    online: !!JARVIS,
    invocation: JARVIS ? [JARVIS.cmd, ...JARVIS.args].join(" ") : null,
    configPath: JARVIS ? JARVIS.configPath : null,
  });
});

app.get("/api/commands", requireJarvis, async (req, res) => {
  try {
    const data = await readConfig();
    res.json(data.commands);
  } catch (e) {
    res.status(500).json({ error: `Couldn't read commands.json: ${e.message}` });
  }
});

app.get("/api/commands/:name", requireJarvis, async (req, res) => {
  try {
    const data = await readConfig();
    const spec = data.commands[req.params.name];
    if (!spec) return res.status(404).json({ error: `No command named "${req.params.name}".` });
    res.json(spec);
  } catch (e) {
    res.status(500).json({ error: `Couldn't read commands.json: ${e.message}` });
  }
});

app.post("/api/commands", requireJarvis, async (req, res) => {
  const { name, spec } = req.body || {};
  try {
    const data = await readConfig();
    const nameErr = validateName(name, { forbidExisting: true, existing: data.commands });
    if (nameErr) return res.status(400).json({ error: nameErr });
    const specErr = validateSpec(spec);
    if (specErr) return res.status(400).json({ error: specErr });

    data.commands[name] = spec;
    await writeConfig(data);
    res.status(201).json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't save command: ${e.message}` });
  }
});

app.put("/api/commands/:name", requireJarvis, async (req, res) => {
  const oldName = req.params.name;
  const { name: newName, spec } = req.body || {};
  try {
    const data = await readConfig();
    if (!Object.prototype.hasOwnProperty.call(data.commands, oldName)) {
      return res.status(404).json({ error: `No command named "${oldName}".` });
    }
    const targetName = newName || oldName;
    if (targetName !== oldName) {
      const nameErr = validateName(targetName, { forbidExisting: true, existing: data.commands });
      if (nameErr) return res.status(400).json({ error: nameErr });
    }
    const specErr = validateSpec(spec);
    if (specErr) return res.status(400).json({ error: specErr });

    if (targetName !== oldName) delete data.commands[oldName];
    data.commands[targetName] = spec;
    await writeConfig(data);
    res.json({ ok: true, name: targetName });
  } catch (e) {
    res.status(500).json({ error: `Couldn't save command: ${e.message}` });
  }
});

app.delete("/api/commands/:name", requireJarvis, async (req, res) => {
  try {
    const data = await readConfig();
    if (!Object.prototype.hasOwnProperty.call(data.commands, req.params.name)) {
      return res.status(404).json({ error: `No command named "${req.params.name}".` });
    }
    delete data.commands[req.params.name];
    await writeConfig(data);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't delete command: ${e.message}` });
  }
});

// One-shot (non-streaming) invocation for quick, no-output-to-watch calls
// like `jarvis ai-clear` \u2014 collects stdout/stderr and resolves when the
// process exits, instead of going through the WebSocket streaming path.
function runJarvisOnce(args, timeoutMs = 10000) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(JARVIS.cmd, [...JARVIS.args, ...args], { windowsHide: true });
    } catch (e) {
      return resolve({ ok: false, error: e.message });
    }
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* ignore */ }
      resolve({ ok: false, error: "timed out" });
    }, timeoutMs);
    child.stdout?.on("data", (d) => (out += d.toString()));
    child.stderr?.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ ok: false, error: e.message });
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve({ ok: code === 0, code, stdout: out.trim(), stderr: err.trim() });
    });
  });
}

app.post("/api/ai/clear", requireJarvis, async (req, res) => {
  const result = await runJarvisOnce(["ai-clear"]);
  if (!result.ok) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't clear history." });
  }
  res.json({ ok: true, message: result.stdout });
});

// `jarvis ai-config` prints (and creates, if missing) the path to
// ai_config.json \u2014 same trick runJarvisOnce already uses for `ai-clear`,
// reused here instead of guessing the path ourselves, so this server never
// needs to duplicate the CLI's own idea of where its files live. Cached
// after the first successful lookup; cleared on /api/reconnect.
async function getAiConfigPath() {
  if (AI_CONFIG_PATH) return AI_CONFIG_PATH;
  const result = await runJarvisOnce(["ai-config"]);
  if (result.ok && result.stdout) {
    AI_CONFIG_PATH = result.stdout.trim();
  }
  return AI_CONFIG_PATH;
}

app.get("/api/ai/raw", requireJarvis, async (req, res) => {
  try {
    const p = await getAiConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate ai_config.json via the jarvis CLI." });
    const text = await fs.readFile(p, "utf-8");
    res.json({ text, path: p });
  } catch (e) {
    res.status(500).json({ error: `Couldn't read ai_config.json: ${e.message}` });
  }
});

app.put("/api/ai/raw", requireJarvis, async (req, res) => {
  const { text } = req.body || {};
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return res.status(400).json({ error: `Invalid JSON: ${e.message}` });
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return res.status(400).json({ error: "Top-level JSON must be an object." });
  }
  if (parsed.providers !== undefined && !Array.isArray(parsed.providers)) {
    return res.status(400).json({ error: "'providers' must be an array." });
  }
  try {
    const p = await getAiConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate ai_config.json via the jarvis CLI." });
    await fs.writeFile(p, text.endsWith("\n") ? text : text + "\n", "utf-8");
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't write ai_config.json: ${e.message}` });
  }
});

async function getPlayniteConfigPath() {
  if (PLAYNITE_CONFIG_PATH) return PLAYNITE_CONFIG_PATH;
  const result = await runJarvisOnce(["playnite-config"]);
  if (result.ok && result.stdout) {
    PLAYNITE_CONFIG_PATH = result.stdout.trim();
  }
  return PLAYNITE_CONFIG_PATH;
}

app.get("/api/playnite/raw", requireJarvis, async (req, res) => {
  try {
    const p = await getPlayniteConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate playnite.json via the jarvis CLI." });
    const text = await fs.readFile(p, "utf-8");
    res.json({ text, path: p });
  } catch (e) {
    res.status(500).json({ error: `Couldn't read playnite.json: ${e.message}` });
  }
});

app.put("/api/playnite/raw", requireJarvis, async (req, res) => {
  const { text } = req.body || {};
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return res.status(400).json({ error: `Invalid JSON: ${e.message}` });
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return res.status(400).json({ error: "Top-level JSON must be an object." });
  }
  try {
    const p = await getPlayniteConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate playnite.json via the jarvis CLI." });
    await fs.writeFile(p, text.endsWith("\n") ? text : text + "\n", "utf-8");
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't write playnite.json: ${e.message}` });
  }
});

async function getSpotifyConfigPath() {
  if (SPOTIFY_CONFIG_PATH) return SPOTIFY_CONFIG_PATH;
  const result = await runJarvisOnce(["spotify-config"]);
  if (result.ok && result.stdout) {
    SPOTIFY_CONFIG_PATH = result.stdout.trim();
  }
  return SPOTIFY_CONFIG_PATH;
}

app.get("/api/spotify/raw", requireJarvis, async (req, res) => {
  try {
    const p = await getSpotifyConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate spotify.json via the jarvis CLI." });
    const text = await fs.readFile(p, "utf-8");
    res.json({ text, path: p });
  } catch (e) {
    res.status(500).json({ error: `Couldn't read spotify.json: ${e.message}` });
  }
});

app.put("/api/spotify/raw", requireJarvis, async (req, res) => {
  const { text } = req.body || {};
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return res.status(400).json({ error: `Invalid JSON: ${e.message}` });
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return res.status(400).json({ error: "Top-level JSON must be an object." });
  }
  try {
    const p = await getSpotifyConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate spotify.json via the jarvis CLI." });
    await fs.writeFile(p, text.endsWith("\n") ? text : text + "\n", "utf-8");
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't write spotify.json: ${e.message}` });
  }
});

async function getMemoryConfigPath() {
  if (MEMORY_CONFIG_PATH) return MEMORY_CONFIG_PATH;
  const result = await runJarvisOnce(["memory-config"]);
  if (result.ok && result.stdout) {
    MEMORY_CONFIG_PATH = result.stdout.trim();
  }
  return MEMORY_CONFIG_PATH;
}

app.get("/api/memory/raw", requireJarvis, async (req, res) => {
  try {
    const p = await getMemoryConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate memory.json via the jarvis CLI." });
    const text = await fs.readFile(p, "utf-8");
    res.json({ text, path: p });
  } catch (e) {
    res.status(500).json({ error: `Couldn't read memory.json: ${e.message}` });
  }
});

app.put("/api/memory/raw", requireJarvis, async (req, res) => {
  const { text } = req.body || {};
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return res.status(400).json({ error: `Invalid JSON: ${e.message}` });
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return res.status(400).json({ error: "Top-level JSON must be an object." });
  }
  if (parsed.facts !== undefined && !Array.isArray(parsed.facts)) {
    return res.status(400).json({ error: "'facts' must be an array." });
  }
  try {
    const p = await getMemoryConfigPath();
    if (!p) return res.status(500).json({ error: "Couldn't locate memory.json via the jarvis CLI." });
    await fs.writeFile(p, text.endsWith("\n") ? text : text + "\n", "utf-8");
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't write memory.json: ${e.message}` });
  }
});

app.get("/api/raw", requireJarvis, async (req, res) => {
  try {
    const text = await fs.readFile(JARVIS.configPath, "utf-8");
    res.json({ text, path: JARVIS.configPath });
  } catch (e) {
    res.status(500).json({ error: `Couldn't read commands.json: ${e.message}` });
  }
});

app.put("/api/raw", requireJarvis, async (req, res) => {
  const { text } = req.body || {};
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return res.status(400).json({ error: `Invalid JSON: ${e.message}` });
  }
  if (!parsed.commands || typeof parsed.commands !== "object" || Array.isArray(parsed.commands)) {
    return res.status(400).json({ error: "Top-level JSON must have a 'commands' object." });
  }
  try {
    suppressWatchUntil = Date.now() + 400;
    await fs.writeFile(JARVIS.configPath, text.endsWith("\n") ? text : text + "\n", "utf-8");
    broadcastCommands(parsed.commands);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't write commands.json: ${e.message}` });
  }
});

const server = createServer(app);

// ---------------------------------------------------------------------------
// WebSocket: live command execution
// ---------------------------------------------------------------------------

const wss = new WebSocketServer({ server, path: "/ws" });

broadcastCommands = (commands) => {
  if (!commands || typeof commands !== "object") return;
  for (const client of wss.clients) {
    send(client, { type: "commands", commands });
  }
};

// Each segment after the first carries its own `mode`: "and" runs it
// alongside whatever came right before it (a parallel batch), anything
// else (including the default, absent, or a stray value) falls back to
// "then" \u2014 wait for the previous batch to finish first. Mirrors the
// CLI's own then/and chain syntax (see jarvis-cli/jarvis/cli.py).
function buildArgv(segments) {
  const argv = [];
  segments.forEach((seg, i) => {
    if (i > 0) argv.push(seg.mode === "and" ? "and" : "then");
    argv.push(seg.name);
    for (const [k, v] of Object.entries(seg.flags || {})) {
      argv.push(`--${k}`, String(v));
    }
  });
  return argv;
}

function killTree(child) {
  if (!child || child.killed) return;
  if (process.platform === "win32") {
    try {
      spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
    } catch {
      /* ignore */
    }
  } else {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      try {
        child.kill("SIGTERM");
      } catch {
        /* ignore */
      }
    }
  }
}

function send(ws, obj) {
  if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(obj));
}

function makeLineBuffer(onLine) {
  let buf = "";
  return (chunk) => {
    buf += chunk.toString();
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) onLine(line.replace(/\r$/, ""));
  };
}

const MAX_ASK_LENGTH = 4000;

// Spawns `jarvis <fullArgs>` and streams its output back over the socket,
// under a caller-chosen set of message type names. Used for both real
// command runs ("run" -> start/stdout/stderr/exit) and AI asks ("ask" ->
// ask-start/ask-stdout/ask-stderr/ask-exit) \u2014 same child-process plumbing
// either way, since an AI ask *is* just `jarvis "<free text>"` under the
// hood (see jarvis-cli/jarvis/cli.py: handle_ai_prompt). Only one of
// either kind runs at a time per connection, tracked via ws.activeChild.
function spawnAndStream(ws, kind, fullArgs, types) {
  let child;
  try {
    child = spawn(JARVIS.cmd, fullArgs, {
      windowsHide: true,
      detached: process.platform !== "win32",
    });
  } catch (e) {
    send(ws, { type: types.error, message: `Couldn't start jarvis: ${e.message}` });
    return;
  }
  ws.activeChild = child;
  ws.activeKind = kind;

  const onOut = makeLineBuffer((line) => send(ws, { type: types.stdout, line }));
  const onErr = makeLineBuffer((line) => send(ws, { type: types.stderr, line }));
  child.stdout.on("data", onOut);
  child.stderr.on("data", onErr);

  child.on("error", (e) => {
    send(ws, { type: types.error, message: `jarvis process error: ${e.message}` });
  });

  child.on("exit", (code, signal) => {
    ws.activeChild = null;
    ws.activeKind = null;
    send(ws, { type: types.exit, code: signal ? null : code, signal: signal || null });
  });
}

const RUN_TYPES = { stdout: "stdout", stderr: "stderr", exit: "exit", error: "error" };
const ASK_TYPES = { stdout: "ask-stdout", stderr: "ask-stderr", exit: "ask-exit", error: "ask-error" };

wss.on("connection", (ws) => {
  ws.activeChild = null;
  ws.activeKind = null; // "run" | "ask", while something is in flight

  if (JARVIS) {
    readConfig()
      .then((data) => send(ws, { type: "commands", commands: data.commands }))
      .catch(() => {});
  }

  ws.on("message", async (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return send(ws, { type: "error", message: "Malformed message." });
    }

    if (msg.type === "cancel") {
      if (ws.activeChild) {
        send(ws, { type: ws.activeKind === "ask" ? "ask-stderr" : "stderr", line: "(abort requested)" });
        killTree(ws.activeChild);
      }
      return;
    }

    if (msg.type === "run") {
      if (!JARVIS) {
        return send(ws, { type: "error", message: "jarvis CLI is not connected." });
      }
      if (ws.activeChild) {
        return send(ws, { type: "error", message: "A command is already running." });
      }
      const segments = Array.isArray(msg.segments) ? msg.segments : [];
      if (segments.length === 0) {
        return send(ws, { type: "error", message: "Nothing to run." });
      }
      for (const seg of segments) {
        if (typeof seg.name !== "string" || /[\r\n\0]/.test(seg.name)) {
          return send(ws, { type: "error", message: "Invalid command name in sequence." });
        }
      }

      const argv = buildArgv(segments);
      const fullArgs = [...JARVIS.args, ...argv];
      send(ws, {
        type: "start",
        cmdline: [JARVIS.cmd, ...fullArgs].join(" "),
        count: segments.length,
      });
      spawnAndStream(ws, "run", fullArgs, RUN_TYPES);
      return;
    }

    if (msg.type === "ask") {
      if (!JARVIS) {
        return send(ws, { type: "ask-error", message: "jarvis CLI is not connected." });
      }
      if (ws.activeChild) {
        return send(ws, { type: "ask-error", message: "Something's already running \u2014 wait for it to finish." });
      }
      const text = typeof msg.text === "string" ? msg.text.trim() : "";
      const quote = typeof msg.quote === "string" ? msg.quote.replace(/\0/g, "").trim() : "";
      if (!text && !quote) {
        return send(ws, { type: "ask-error", message: "Nothing to ask." });
      }
      if (text.length > MAX_ASK_LENGTH) {
        return send(ws, { type: "ask-error", message: `Keep it under ${MAX_ASK_LENGTH} characters.` });
      }
      if (text && /[\r\n\0]/.test(text)) {
        return send(ws, { type: "ask-error", message: "Ask can't contain newlines." });
      }

      let prompt = text;
      if (quote) {
        const clipped = quote.slice(0, 4000);
        prompt = (
          "The user highlighted this excerpt from the conversation and wants you to address it specifically:\n" +
          '"""\n' + clipped + "\n" +
          '"""\n\n' +
          (text || "Please respond about the quoted excerpt.")
        );
      }
      if (prompt.length > MAX_ASK_LENGTH + 4500) {
        return send(ws, { type: "ask-error", message: "That quote plus message is too long." });
      }

      if (msg.redo) {
        await runJarvisOnce(["ai-drop-from", text]);
      }

      // A single argv element, exactly like typing `jarvis "<text>"` at a
      // real shell \u2014 spawn() takes argv directly (no shell involved), so
      // this is one argument no matter how much whitespace or punctuation
      // it contains, with no injection risk.
      const fullArgs = [...JARVIS.args, prompt];
      send(ws, { type: "ask-start", cmdline: [JARVIS.cmd, ...JARVIS.args, "<your message>"].join(" ") });
      spawnAndStream(ws, "ask", fullArgs, ASK_TYPES);
      return;
    }
  });

  ws.on("close", () => {
    if (ws.activeChild) killTree(ws.activeChild);
  });
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

resolveJarvis().then((result) => {
  JARVIS = result;
  startConfigWatcher();
  server.listen(PORT, HOST, () => {
    console.log(`\n  J A R V I S  web UI running at http://${HOST}:${PORT}\n`);
    if (JARVIS) {
      console.log(`  linked to jarvis via: ${[JARVIS.cmd, ...JARVIS.args].join(" ")}`);
      console.log(`  config: ${JARVIS.configPath}\n`);
    } else {
      console.log(
        "  WARNING: couldn't find the jarvis CLI (tried jarvis, python3 -m jarvis, python -m jarvis, py -m jarvis)."
      );
      console.log("  Install it first (pip install .), or set JARVIS_BIN, then reload the page.\n");
    }
  });
});
