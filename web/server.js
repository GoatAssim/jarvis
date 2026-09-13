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
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT) || 4173;
const HOST = "127.0.0.1";

const RESERVED_NAMES = new Set(["config", "ai-config", "ai-clear", "ai-drop-from", "playnite-config", "spotify-config", "spotify-login", "memory-config", "everything-config", "tools-list", "tool-run", "tool-preview", "tool-safety-set", "conv-new", "conv-list", "conv-show", "conv-switch", "conv-delete", "logs", "logs-list", "logs-show", "logs-clear", "organize-json", "then", "and", "-h", "--help"]);

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
  for (const field of ["confirm_required", "ai_review"]) {
    if (spec[field] !== undefined && typeof spec[field] !== "boolean") {
      return `'${field}' must be true or false.`;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(
  express.json({
    limit: "1mb",
    // Stash the exact raw bytes we received before body-parser tries to
    // JSON.parse them, so if parsing fails we can log what actually came
    // in (headers + raw body) instead of just the generic SyntaxError.
    // Temporary diagnostic for tracking down the "Unexpected non-whitespace
    // character" errors — safe to remove later, doesn't change parsing
    // behavior for valid requests.
    verify: (req, _res, buf) => {
      req.rawBody = buf;
    },
  })
);
// express.json() throws a SyntaxError (not caught anywhere) when a request
// body isn't valid JSON. Without this handler that error propagates as an
// unhandled exception and gets dumped to the console as a stack trace on
// every malformed request. Catch it here, log what we can about the
// offending request, and reply with a clean 400 instead — this only
// affects requests with bad JSON bodies, everything else is untouched.
app.use((err, req, res, next) => {
  if (err && err.type === "entity.parse.failed") {
    console.warn(
      `[bad-json-body] ${new Date().toISOString()} ${req.method} ${req.originalUrl}\n` +
        `  from: ${req.ip}\n` +
        `  headers: ${JSON.stringify(req.headers)}\n` +
        `  raw body (${req.rawBody ? req.rawBody.length : 0} bytes): ${JSON.stringify(
          req.rawBody ? req.rawBody.toString("utf-8") : ""
        )}`
    );
    return res.status(400).json({ error: "Invalid JSON body." });
  }
  next(err);
});
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
function runJarvisOnce(args, timeoutMs = 10000, extraEnv = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(JARVIS.cmd, [...JARVIS.args, ...args], {
        windowsHide: true,
        env: { ...process.env, ...extraEnv },
      });
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


// Conversation ids are jarvis-cli's secrets.token_hex(8) — 16 lowercase hex
// chars. Validated the same way here as in conversations.py before ever
// being interpolated into a `jarvis ...` argv or env var.
const CONVERSATION_ID_RE = /^[a-f0-9]{8,64}$/;

function isValidConversationId(id) {
  return typeof id === "string" && CONVERSATION_ID_RE.test(id);
}

function conversationEnv(id) {
  return isValidConversationId(id) ? { JARVIS_CONVERSATION_ID: id } : {};
}

app.post("/api/ai/clear", requireJarvis, async (req, res) => {
  const conversationId = typeof req.body?.conversationId === "string" ? req.body.conversationId : "";
  const result = await runJarvisOnce(["ai-clear"], 10000, conversationEnv(conversationId));
  if (!result.ok) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't clear history." });
  }
  res.json({ ok: true, message: result.stdout });
});

// Providers eligible for the Ask panel's "--provider" override picker (see
// jarvis-provider-override.patch / ai_client._eligible_providers). Reads
// ai_config.json directly and mirrors _eligible_providers()'s "enabled, and
// either ollama or has a real key" filter plus _sort_providers_by_priority's
// ordering \u2014 kept in sync with those two functions by hand since this is
// JS re-deriving Python logic, not calling it. Deliberately never returns
// api_keys/api_key themselves, only enough to label a picker entry: this
// is a config *summary* for the UI, not the raw file (that's what the
// Settings > Config > AI tab's /api/config/file/ai_config.json/raw is for).
app.get("/api/ai/providers", requireJarvis, async (req, res) => {
  const full = path.join(jarvisConfigDir(), "ai_config.json");
  let parsed;
  try {
    parsed = JSON.parse(await fs.readFile(full, "utf-8"));
  } catch (e) {
    return res.status(404).json({ error: `Couldn't read ai_config.json: ${e.message}` });
  }
  const providers = Array.isArray(parsed?.providers) ? parsed.providers : [];
  const defaults = parsed?.defaults && typeof parsed.defaults === "object" ? parsed.defaults : {};

  const hasRealKey = (p) => {
    const keys = Array.isArray(p.api_keys) ? p.api_keys.filter((k) => typeof k === "string" && k.trim()) : [];
    if (keys.length) return true;
    return typeof p.api_key === "string" && p.api_key.trim().length > 0;
  };
  const eligible = providers.filter((p) => (
    p && typeof p === "object" && p.enabled !== false && (p.type === "ollama" || hasRealKey(p))
  ));

  const priorityList = Array.isArray(defaults.provider_priority) ? defaults.provider_priority : [];
  const rank = new Map(priorityList.filter((n) => typeof n === "string").map((n, i) => [n, i]));
  const trailing = rank.size;
  const ordered = eligible
    .map((p, i) => ({ p, i }))
    .sort((a, b) => {
      const ra = rank.has(a.p.name) ? rank.get(a.p.name) : trailing + a.i;
      const rb = rank.has(b.p.name) ? rank.get(b.p.name) : trailing + b.i;
      return ra - rb || a.i - b.i;
    })
    .map(({ p }) => ({ name: p.name || p.type || "provider", model: p.model || null, type: p.type || null }));

  res.json({ providers: ordered });
});

// Prompt "capacity" mode — 400%/100%/50% (full/compact/ultra), see
// ai_client.PROMPT_MODES. Thin wrapper over `jarvis mode` / `jarvis
// mode-set`, same pattern as everything else here: the actual state lives
// in ~/.jarvis/ai_config.json on the machine running jarvis-cli, this just
// shells out and relays the JSON.
app.get("/api/mode", requireJarvis, async (req, res) => {
  const result = await runJarvisOnce(["mode"], 10000);
  if (!result.ok) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't read mode." });
  }
  try {
    res.json(JSON.parse(result.stdout));
  } catch {
    res.status(500).json({ error: "Couldn't parse mode output." });
  }
});

app.post("/api/mode", requireJarvis, async (req, res) => {
  const mode = typeof req.body?.mode === "string" ? req.body.mode.trim() : "";
  if (!mode) return res.status(400).json({ error: "mode is required" });
  const result = await runJarvisOnce(["mode-set", mode], 10000);
  if (!result.ok) {
    let parsed = null;
    try { parsed = JSON.parse(result.stdout); } catch { /* not JSON */ }
    return res.status(400).json({ error: (parsed && parsed.error) || result.error || result.stderr || "Couldn't set mode." });
  }
  try {
    res.json(JSON.parse(result.stdout));
  } catch {
    res.status(500).json({ error: "Couldn't parse mode output." });
  }
});

// ---------------------------------------------------------------------------
// Conversations — every one lives in ~/.jarvis/conversations on the
// machine running jarvis-cli (see conversations.py), never anywhere else.
// The web UI is just a thin client over `jarvis conv-*`, same pattern as
// the tool debug dashboard's `jarvis tool-run`.
// ---------------------------------------------------------------------------

app.get("/api/conversations", requireJarvis, async (req, res) => {
  const q = typeof req.query.q === "string" ? req.query.q.trim() : "";
  const args = q ? ["conv-list", q] : ["conv-list"];
  const result = await runJarvisOnce(args, 10000);
  if (!result.ok) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't list conversations." });
  }
  try {
    res.json(JSON.parse(result.stdout));
  } catch (e) {
    res.status(500).json({ error: `Couldn't parse conv-list: ${e.message}` });
  }
});

app.post("/api/conversations", requireJarvis, async (req, res) => {
  const title = typeof req.body?.title === "string" ? req.body.title.trim() : "";
  const args = title ? ["conv-new", title] : ["conv-new"];
  const result = await runJarvisOnce(args, 10000);
  if (!result.ok) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't start a new conversation." });
  }
  try {
    res.json(JSON.parse(result.stdout));
  } catch (e) {
    res.status(500).json({ error: `Couldn't parse conv-new: ${e.message}` });
  }
});

app.get("/api/conversations/:id", requireJarvis, async (req, res) => {
  if (!isValidConversationId(req.params.id)) {
    return res.status(400).json({ error: "Invalid conversation id." });
  }
  const result = await runJarvisOnce(["conv-show", req.params.id], 10000);
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch { /* fall through to generic error below */ }
  if (parsed && parsed.error) {
    return res.status(404).json(parsed);
  }
  if (!result.ok || !parsed) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't load conversation." });
  }
  res.json(parsed);
});

app.delete("/api/conversations/:id", requireJarvis, async (req, res) => {
  if (!isValidConversationId(req.params.id)) {
    return res.status(400).json({ error: "Invalid conversation id." });
  }
  const result = await runJarvisOnce(["conv-delete", req.params.id], 10000);
  try {
    const parsed = JSON.parse(result.stdout);
    if (!parsed.ok) return res.status(404).json(parsed);
    return res.json(parsed);
  } catch (e) {
    res.status(500).json({ error: result.error || result.stderr || `Couldn't delete conversation: ${e.message}` });
  }
});

// ---------------------------------------------------------------------------
// Logs — raw request/response/tool traffic between the model and this
// backend, one append-only file per conversation under ~/.jarvis/logs/
// (see logs.py). Same thin-client pattern as /api/conversations above:
// the web UI never reads the files itself, it just shells out to the
// non-interactive `jarvis logs-*` commands and forwards their JSON.
// ---------------------------------------------------------------------------

app.get("/api/logs", requireJarvis, async (req, res) => {
  const result = await runJarvisOnce(["logs-list"], 10000);
  if (!result.ok) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't list logs." });
  }
  try {
    res.json(JSON.parse(result.stdout));
  } catch (e) {
    res.status(500).json({ error: `Couldn't parse logs-list: ${e.message}` });
  }
});

app.get("/api/logs/:id", requireJarvis, async (req, res) => {
  if (!isValidConversationId(req.params.id)) {
    return res.status(400).json({ error: "Invalid conversation id." });
  }
  const limitRaw = typeof req.query.limit === "string" ? req.query.limit.trim() : "";
  const limit = /^\d+$/.test(limitRaw) ? limitRaw : "50";
  const result = await runJarvisOnce(["logs-show", req.params.id, limit], 10000);
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch { /* fall through to generic error below */ }
  if (parsed && parsed.error) {
    return res.status(404).json(parsed);
  }
  if (!result.ok || !parsed) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't load logs." });
  }
  res.json(parsed);
});

app.delete("/api/logs/:id", requireJarvis, async (req, res) => {
  if (!isValidConversationId(req.params.id)) {
    return res.status(400).json({ error: "Invalid conversation id." });
  }
  const result = await runJarvisOnce(["logs-clear", req.params.id], 10000);
  try {
    const parsed = JSON.parse(result.stdout);
    if (!parsed.ok) return res.status(404).json(parsed);
    return res.json(parsed);
  } catch (e) {
    res.status(500).json({ error: result.error || result.stderr || `Couldn't clear log: ${e.message}` });
  }
});

app.get("/api/tools", requireJarvis, async (req, res) => {
  const result = await runJarvisOnce(["tools-list"], 15000);
  if (!result.ok) {
    return res.status(500).json({ error: result.error || result.stderr || "Couldn't list tools." });
  }
  try {
    res.json(JSON.parse(result.stdout));
  } catch (e) {
    res.status(500).json({ error: `Couldn't parse tools-list: ${e.message}` });
  }
});

// Debug dashboard: run one AI tool directly (bypassing the model) and
// return exactly what it returns. `jarvis tool-run` takes argv directly
// (spawn, no shell) so the JSON-stringified arguments never need escaping.
app.post("/api/tools/run", requireJarvis, async (req, res) => {
  const name = typeof req.body?.name === "string" ? req.body.name.trim() : "";
  if (!name) {
    return res.status(400).json({ error: "Missing tool name." });
  }
  let argsJson;
  try {
    argsJson = JSON.stringify(req.body?.arguments && typeof req.body.arguments === "object" ? req.body.arguments : {});
  } catch (e) {
    return res.status(400).json({ error: `Couldn't serialize arguments: ${e.message}` });
  }
  // Optional local-only capacity-mode override for this run (the debug
  // panel's own mode switch, separate from the app's real global mode —
  // see /api/mode above). `jarvis tool-run` validates it against the real
  // PROMPT_MODES itself and silently ignores anything it doesn't
  // recognize, so no validation is needed here beyond "it's a string" —
  // an empty string is the "no override" case it already expects.
  const mode = typeof req.body?.mode === "string" ? req.body.mode.trim() : "";
  const result = await runJarvisOnce(["tool-run", name, argsJson, mode], 30000, { JARVIS_UI: "web" });
  // `jarvis tool-run` always prints a JSON object to stdout, even on its
  // own validation errors (bad JSON args, missing name), just with a
  // non-zero exit code in those cases — so try parsing stdout first no
  // matter what the exit code was, and only fall back to a generic error.
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch { /* not JSON — fall through */ }
  if (parsed !== undefined) {
    return res.json({ ok: result.ok, result: parsed, raw: result.stdout, stderr: result.stderr });
  }
  res.status(500).json({ error: result.error || result.stderr || "Tool run failed.", raw: result.stdout, stderr: result.stderr });
});

// Debug dashboard: check whether a tool call would be gated by a
// confirmation prompt, and (if AI review is on for it) get a second
// provider's risk note — without actually running the tool. The RUN
// button calls this first; only a follow-up call to /api/tools/run
// (above), after the user clicks "Yes", actually executes anything.
app.post("/api/tools/preview", requireJarvis, async (req, res) => {
  const name = typeof req.body?.name === "string" ? req.body.name.trim() : "";
  if (!name) {
    return res.status(400).json({ error: "Missing tool name." });
  }
  let argsJson;
  try {
    argsJson = JSON.stringify(req.body?.arguments && typeof req.body.arguments === "object" ? req.body.arguments : {});
  } catch (e) {
    return res.status(400).json({ error: `Couldn't serialize arguments: ${e.message}` });
  }
  // Optional local-only capacity-mode override for this one AI-review call
  // (the debug panel's own mode switch, kept separate from the app's real
  // global mode — see /api/mode above). `jarvis tool-preview` validates it
  // against the real PROMPT_MODES itself and silently ignores anything it
  // doesn't recognize, so no validation is needed here beyond "it's a
  // string" — an empty string is the "no override" case it already expects.
  const mode = typeof req.body?.mode === "string" ? req.body.mode.trim() : "";

  // Generous timeout: when ai_review is on this makes a real network call
  // to a second AI provider before responding.
  const result = await runJarvisOnce(["tool-preview", name, argsJson, mode], 30000, { JARVIS_UI: "web" });
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch { /* not JSON — fall through */ }
  if (parsed !== undefined) {
    return res.json(parsed);
  }
  res.status(500).json({ error: result.error || result.stderr || "Tool preview failed." });
});

// Debug dashboard: flip one of a tool's two safety toggles (confirm_required
// or ai_review — see jarvis-cli/jarvis/tool_safety.py). Returns the tool's
// full updated flag set.
app.post("/api/tools/safety", requireJarvis, async (req, res) => {
  const name = typeof req.body?.name === "string" ? req.body.name.trim() : "";
  const key = typeof req.body?.key === "string" ? req.body.key.trim() : "";
  if (!name || !["confirm_required", "ai_review"].includes(key)) {
    return res.status(400).json({ error: "Missing or invalid name/key." });
  }
  const value = req.body?.value ? "true" : "false";
  const result = await runJarvisOnce(["tool-safety-set", name, key, value], 15000);
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch { /* not JSON — fall through */ }
  if (parsed !== undefined && !parsed.error) {
    return res.json(parsed);
  }
  res.status(500).json({ error: (parsed && parsed.error) || result.error || result.stderr || "Couldn't update tool safety flag." });
});

// ---------------------------------------------------------------------------
// Generic config file browser (the Settings > Config tabs).
//
// Instead of one hardcoded REST pair per config file, this lists whatever
// *.json files actually sit directly in the jarvis config directory (the
// same directory `jarvis config` resolves commands.json into) and serves
// any of them generically. Add a new *.json file to that directory later
// (a new tool's own config) and it shows up in the Config tab immediately
// \u2014 nothing here needs to change.
//
// Known filenames get a nicer label/hint and an extra shape check beyond
// "is this valid JSON" (mirroring the checks this project always ran for
// ai_config.json/commands.json/memory.json). Anything else still works \u2014
// it just gets a generic label and only the base JSON-validity check.
// ---------------------------------------------------------------------------

const KNOWN_CONFIGS = {
  "commands.json": {
    label: "Commands",
    hint: "The entire commands.json file \u2014 every command at once. Useful for bulk edits or pasting a config from elsewhere. Changes bypass the builder's per-field checks; jarvis validates when you run a command.",
    validate(parsed) {
      if (!parsed.commands || typeof parsed.commands !== "object" || Array.isArray(parsed.commands)) {
        return "Top-level JSON must have a 'commands' object.";
      }
      return null;
    },
  },
  "ai_config.json": {
    label: "AI",
    hint: "Persona, defaults, and every AI provider. Add multiple keys to a provider's api_keys array for failover. Read fresh on every ask \u2014 nothing to restart after saving.",
    validate(parsed) {
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return "Top-level JSON must be an object.";
      }
      if (parsed.providers !== undefined && !Array.isArray(parsed.providers)) {
        return "'providers' must be an array.";
      }
      return null;
    },
  },
  "playnite.json": {
    label: "Playnite",
    hint: "Playnite Bridge token, base URL, and frequent-game cache. Copy the token from Playnite: Main Menu \u2192 Playnite Bridge. Set enabled to false to disable integration.",
  },
  "spotify.json": {
    label: "Spotify",
    hint: "Spotify Developer client_id (and optional secret). Redirect URI must be http://127.0.0.1:19823/callback. After saving, run jarvis spotify-login in a terminal and sign in with the same account as the Spotify app on this PC. Tokens are stored here after login.",
  },
  "memory.json": {
    label: "Memory",
    hint: "Long-term facts Jarvis keeps across chats. Only facts that match the current question are added to the prompt. Chat history is short-term and ai-clear does not wipe this. Each item is id, optional key, and fact.",
    validate(parsed) {
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return "Top-level JSON must be an object.";
      }
      if (parsed.facts !== undefined && !Array.isArray(parsed.facts)) {
        return "'facts' must be an array.";
      }
      return null;
    },
  },
  "tool_safety.json": {
    label: "Tool Safety",
    hint: "Per-tool confirm_required / ai_review flags. Usually easier to flip from the Debug dashboard, but this is the raw file.",
  },
};

function jarvisConfigDir() {
  return JARVIS?.configPath ? path.dirname(JARVIS.configPath) : path.join(os.homedir(), ".jarvis");
}

function prettyConfigLabel(filename) {
  return filename
    .replace(/\.json$/i, "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// Resolves a config filename from the URL to a real path, refusing anything
// that isn't a plain "<name>.json" sitting directly inside the config dir
// \u2014 no traversal, no reaching into subdirectories (screenshots/,
// conversations/ stay hidden from this browser).
function resolveConfigFile(rawName) {
  const name = path.basename(String(rawName || ""));
  if (!/^[A-Za-z0-9._-]+\.json$/i.test(name)) return null;
  const dir = jarvisConfigDir();
  const full = path.join(dir, name);
  if (path.dirname(full) !== dir) return null;
  return { name, full };
}

app.get("/api/config/list", requireJarvis, async (req, res) => {
  const dir = jarvisConfigDir();
  try {
    const entries = await fs.readdir(dir, { withFileTypes: true });
    const order = Object.keys(KNOWN_CONFIGS);
    const files = entries
      .filter((e) => e.isFile() && /\.json$/i.test(e.name))
      .map((e) => e.name)
      .sort((a, b) => {
        const ai = order.indexOf(a), bi = order.indexOf(b);
        if (ai !== -1 || bi !== -1) return (ai === -1 ? order.length : ai) - (bi === -1 ? order.length : bi);
        return a.localeCompare(b);
      })
      .map((name) => ({
        name,
        label: KNOWN_CONFIGS[name]?.label || prettyConfigLabel(name),
        hint: KNOWN_CONFIGS[name]?.hint || "Auto-discovered config file \u2014 jarvis only checks that it's well-formed JSON on save.",
        path: path.join(dir, name),
      }));
    res.json({ dir, files });
  } catch (e) {
    res.status(500).json({ error: `Couldn't list ${dir}: ${e.message}` });
  }
});

app.get("/api/config/file/:name/raw", requireJarvis, async (req, res) => {
  const resolved = resolveConfigFile(req.params.name);
  if (!resolved) return res.status(400).json({ error: "Invalid config file name." });
  try {
    const text = await fs.readFile(resolved.full, "utf-8");
    res.json({ text, path: resolved.full, name: resolved.name });
  } catch (e) {
    res.status(404).json({ error: `Couldn't read ${resolved.name}: ${e.message}` });
  }
});

app.put("/api/config/file/:name/raw", requireJarvis, async (req, res) => {
  const resolved = resolveConfigFile(req.params.name);
  if (!resolved) return res.status(400).json({ error: "Invalid config file name." });
  const { text } = req.body || {};
  if (typeof text !== "string") {
    return res.status(400).json({ error: "Missing 'text'." });
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return res.status(400).json({ error: `Invalid JSON: ${e.message}` });
  }
  const known = KNOWN_CONFIGS[resolved.name];
  if (known?.validate) {
    const problem = known.validate(parsed);
    if (problem) return res.status(400).json({ error: problem });
  }
  try {
    if (resolved.name === "commands.json") suppressWatchUntil = Date.now() + 400;
    await fs.writeFile(resolved.full, text.endsWith("\n") ? text : text + "\n", "utf-8");
    if (resolved.name === "commands.json" && parsed.commands) broadcastCommands(parsed.commands);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: `Couldn't write ${resolved.name}: ${e.message}` });
  }
});

// ---------------------------------------------------------------------------
// organize-json \u2014 turns "organize-json <path>" typed into the Ask box into
// a local file read + parse, never a prompt to any AI provider (so it never
// costs a token, no matter how big the file is). Delegates the actual
// parsing/error-reporting to the CLI (`jarvis organize-json --json`) so the
// web UI and terminal share one implementation of "what's wrong with this
// JSON" and "what does an organized view of it look like".
// ---------------------------------------------------------------------------

app.post("/api/json/organize", requireJarvis, async (req, res) => {
  const { path: rawPath } = req.body || {};
  if (!rawPath || typeof rawPath !== "string" || !rawPath.trim()) {
    return res.status(400).json({ error: "Missing 'path'." });
  }
  const result = await runJarvisOnce(["organize-json", rawPath.trim(), "--json"]);
  if (!result.stdout) {
    return res.status(500).json({ error: result.stderr || result.error || "organize-json produced no output." });
  }
  let payload;
  try {
    payload = JSON.parse(result.stdout);
  } catch (e) {
    return res.status(500).json({ error: `Unexpected output from jarvis: ${e.message}` });
  }
  if (!payload.ok) {
    return res.status(400).json(payload);
  }
  res.json(payload);
});

const SCREENSHOT_NAME_RE = /^ss_[A-Za-z0-9_.-]+\.png$/;

function screenshotsDir() {
  return path.join(os.homedir(), ".jarvis", "screenshots");
}

app.get("/api/screenshots/:name", (req, res) => {
  const name = path.basename(String(req.params.name || ""));
  if (!SCREENSHOT_NAME_RE.test(name)) {
    return res.status(400).json({ error: "Invalid screenshot name." });
  }
  const filePath = path.join(screenshotsDir(), name);
  res.sendFile(filePath, (err) => {
    if (err && !res.headersSent) {
      res.status(404).json({ error: "Screenshot not found." });
    }
  });
});

// yt-dlp downloads (see jarvis-cli/jarvis/ytdl_tools.py) each land in their
// own job folder under ~/.jarvis/downloads/<jobId>/<filename> — same
// basename-only + no-traversal treatment as the screenshots route above,
// just keyed by (jobId, filename) instead of a single flat name since a
// job's actual output filename comes from the video's own title.
const DOWNLOAD_JOB_RE = /^dl_[A-Za-z0-9_-]+$/;

function downloadsDir() {
  return path.join(os.homedir(), ".jarvis", "downloads");
}

app.get("/api/downloads/:jobId/:filename", (req, res) => {
  const jobId = path.basename(String(req.params.jobId || ""));
  const filename = path.basename(String(req.params.filename || ""));
  if (!DOWNLOAD_JOB_RE.test(jobId) || !filename) {
    return res.status(400).json({ error: "Invalid download reference." });
  }
  const jobDir = path.join(downloadsDir(), jobId);
  const filePath = path.join(jobDir, filename);
  if (path.dirname(filePath) !== jobDir) {
    return res.status(400).json({ error: "Invalid download reference." });
  }
  res.sendFile(filePath, (err) => {
    if (err && !res.headersSent) {
      res.status(404).json({ error: "Download not found." });
    }
  });
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
  const feed = (chunk) => {
    buf += chunk.toString();
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) onLine(line.replace(/\r$/, ""));
  };
  feed.flush = () => {
    if (buf) {
      onLine(buf.replace(/\r$/, ""));
      buf = "";
    }
  };
  return feed;
}

const MAX_ASK_LENGTH = 4000;

// Spawns `jarvis <fullArgs>` and streams its output back over the socket,
// under a caller-chosen set of message type names. Used for both real
// command runs ("run" -> start/stdout/stderr/exit) and AI asks ("ask" ->
// ask-start/ask-stdout/ask-stderr/ask-exit) \u2014 same child-process plumbing
// either way, since an AI ask *is* just `jarvis "<free text>"` under the
// hood (see jarvis-cli/jarvis/cli.py: handle_ai_prompt). Only one of
// either kind runs at a time per connection, tracked via ws.activeChild.
//
// onStdoutLine(line), if given, gets first look at every stdout line before
// it's forwarded as a normal stdout message — return true to swallow the
// line, falsy to let it through as usual. Used by the "ask" flow to catch
// "JARVIS_CONFIRM_REQUEST {...}" lines (see cli.py's on_confirm_request)
// and turn them into ask-confirm-request instead of chat-bubble text.
function spawnAndStream(ws, kind, fullArgs, types, extraEnv = {}, onStdoutLine = null) {
  let child;
  try {
    child = spawn(JARVIS.cmd, fullArgs, {
      windowsHide: true,
      detached: process.platform !== "win32",
      env: {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
        ...extraEnv,
      },
    });
  } catch (e) {
    send(ws, { type: types.error, message: `Couldn't start jarvis: ${e.message}` });
    return;
  }
  ws.activeChild = child;
  ws.activeKind = kind;

  const onOut = makeLineBuffer((line) => {
    if (onStdoutLine && onStdoutLine(line)) return;
    send(ws, { type: types.stdout, line });
  });
  const onErr = makeLineBuffer((line) => send(ws, { type: types.stderr, line }));
  child.stdout.on("data", onOut);
  child.stderr.on("data", onErr);

  child.on("error", (e) => {
    send(ws, { type: types.error, message: `jarvis process error: ${e.message}` });
  });

  child.on("exit", (code, signal) => {
    onOut.flush();
    onErr.flush();
    ws.activeChild = null;
    ws.activeKind = null;
    send(ws, { type: types.exit, code: signal ? null : code, signal: signal || null });
  });
}

const RUN_TYPES = { stdout: "stdout", stderr: "stderr", exit: "exit", error: "error" };
const ASK_TYPES = { stdout: "ask-stdout", stderr: "ask-stderr", exit: "ask-exit", error: "ask-error" };
const CONFIRM_MARKER = "JARVIS_CONFIRM_REQUEST ";
// Phase 0 (new_plan.md): cli.py prints one of these per successful ask —
// {input_tokens, output_tokens, total_tokens, rounds: [...], tool_calls: [...]}
// (see ai_providers.get_usage_summary). Same marker-on-stdout protocol as
// CONFIRM_MARKER above, forwarded to the browser as "ask-usage" so the
// debug menu / chat UI can render a per-turn token breakdown.
const USAGE_MARKER = "JARVIS_USAGE ";

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
      // Same "JARVIS_CONFIRM_REQUEST {...}" protocol as the ask flow below
      // (see cli.py's confirm_tool_call / confirm_direct_command) — a
      // directly-run saved command can carry its own confirm_required/
      // ai_review flags, independent of anything the AI does, so this
      // flow needs the exact same marker-detection + pause-for-answer
      // handling, just under "confirm-request" instead of
      // "ask-confirm-request" so the UI can tell which surface asked.
      const runOnStdoutLine = (line) => {
        if (!line.startsWith(CONFIRM_MARKER)) return false;
        let payload;
        try {
          payload = JSON.parse(line.slice(CONFIRM_MARKER.length));
        } catch {
          return false;
        }
        send(ws, {
          type: "confirm-request",
          tool: payload.tool,
          arguments: payload.arguments || {},
          risk_note: payload.risk_note || null,
        });
        return true;
      };
      spawnAndStream(ws, "run", fullArgs, RUN_TYPES, {}, runOnStdoutLine);
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
      if (text && /\0/.test(text)) {
        return send(ws, { type: "ask-error", message: "Ask can't contain a null byte." });
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
        await runJarvisOnce(["ai-drop-from", text], 10000, conversationEnv(msg.conversationId));
      }

      // A single argv element, exactly like typing `jarvis "<text>"` at a
      // real shell \u2014 spawn() takes argv directly (no shell involved), so
      // this is one argument no matter how much whitespace or punctuation
      // it contains, with no injection risk.
      const fullArgs = [...JARVIS.args, prompt];
      send(ws, { type: "ask-start", cmdline: [JARVIS.cmd, ...JARVIS.args, "<your message>"].join(" ") });
      const extraEnv = { ...conversationEnv(msg.conversationId), JARVIS_UI: "web" };
      if (typeof msg.allowedTools === "string") {
        extraEnv.JARVIS_ALLOWED_TOOLS = msg.allowedTools;
      }
      // Ask panel's provider-override picker (see /api/ai/providers above
      // and cli.py's JARVIS_PROVIDER_OVERRIDE handling) — a provider name
      // only, never trusted as a path/shell fragment; same conservative
      // shape-check as JARVIS_ALLOWED_TOOLS above. Absent/invalid just
      // means "no override", identical to before this existed.
      if (typeof msg.provider === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(msg.provider)) {
        extraEnv.JARVIS_PROVIDER_OVERRIDE = msg.provider;
      }
      const onStdoutLine = (line) => {
        if (line.startsWith(USAGE_MARKER)) {
          let usage;
          try {
            usage = JSON.parse(line.slice(USAGE_MARKER.length));
          } catch {
            return false; // malformed — let it through as plain text rather than swallow it silently
          }
          send(ws, { type: "ask-usage", usage });
          return true;
        }
        if (!line.startsWith(CONFIRM_MARKER)) return false;
        let payload;
        try {
          payload = JSON.parse(line.slice(CONFIRM_MARKER.length));
        } catch {
          return false; // malformed — let it through as plain text rather than swallow it silently
        }
        send(ws, {
          type: "ask-confirm-request",
          tool: payload.tool,
          arguments: payload.arguments || {},
          risk_note: payload.risk_note || null,
        });
        return true;
      };
      spawnAndStream(ws, "ask", fullArgs, ASK_TYPES, extraEnv, onStdoutLine);
      return;
    }

    if (msg.type === "ask-confirm-response" || msg.type === "confirm-response") {
      // The user clicked Yes/No on a confirmation prompt the running
      // ask OR direct command-run is blocked waiting on (see cli.py's
      // on_confirm_request/confirm_tool_call — it's doing a blocking
      // sys.stdin.readline() right now). Write the answer straight to
      // its stdin; it picks up on the very next line. Works for either
      // kind since both go through the exact same stdin-prompt protocol.
      if (!ws.activeChild) return;
      if (msg.type === "ask-confirm-response" && ws.activeKind !== "ask") return;
      if (msg.type === "confirm-response" && ws.activeKind !== "run") return;
      try {
        ws.activeChild.stdin.write((msg.approved ? "y" : "n") + "\n");
      } catch {
        /* child may have already exited — nothing to do */
      }
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