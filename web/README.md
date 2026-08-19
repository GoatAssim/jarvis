# JARVIS Web Console

A local web dashboard for the [`jarvis`](../jarvis-cli) CLI. It doesn't
reimplement jarvis — it reads and writes your real `commands.json` and
spawns your real `jarvis` binary, and streams its actual output back to
the browser. Anything you can do with `jarvis` on the command line, you
can do here: run commands, chain them with `then`, talk to its AI mode,
and create, edit, or hand-edit the config.

<p align="center"><em>Dark HUD, arc-reactor cyan, scanning boot sequence — built to feel like the console it's named after.</em></p>

## Quick start

```bash
cd web
npm install
npm start
```

Then open **http://127.0.0.1:4173**.

You need [`jarvis`](../jarvis-cli) installed and on PATH first (`pip
install .` from `jarvis-cli/`) — the console is a front end for it, not a
replacement. If the CLI isn't found, the console still loads in a clearly
marked **offline** state and tells you what to do; nothing crashes.

**Requirements:** Node.js 18 or later. No other services, databases, or
accounts — everything lives in `~/.jarvis/commands.json`, same as the CLI.

## What it does

- **Run any command.** Pick a command from the list, fill in a
  form generated from its `vars` (required ones are marked, optional ones
  show their default), and hit Execute. Press Enter in the last field to
  run without reaching for the mouse.
- **Chain commands.** Queue several commands into a sequence bar and run
  them as one `jarvis a then b and c` call, exactly like the CLI's own
  `then`/`and` syntax — same one-process semantics, including the option
  to run two queued commands together instead of one after another. Click
  the connector between two queued items to flip it between "→ then" (wait
  for the previous one) and "∥ and" (run alongside it).
- **Watch it run, live.** Output streams over a WebSocket line by line as
  the real process produces it, styled by stream (stdout/stderr) with a
  clear exit banner. **Abort** sends a real termination signal to the
  whole process tree, not just the parent.
- **Build commands without hand-writing JSON.** The command builder has a
  form for the description and variables, a reorderable list of steps
  (drag to reorder, add/remove, mark `continueOnError`, run a step in
  parallel with the one before it, or hide a step's command line from the
  output), and a condition editor for `if`/`unless` — either simple `var
  equals value` rows or the full string-expression syntax the CLI
  supports. A live **Raw JSON** tab mirrors the same spec both ways, so
  you can switch to hand-editing a single command at any point.
- **Edit the whole config at once.** The **{ } Config** button opens the
  entire `commands.json`, not just one command — for reordering, bulk
  edits, or pasting in a config from elsewhere. This is the same file the
  CLI's own docs point you to for hand-editing, just in the browser, with
  a Reload button and inline error reporting if something doesn't parse.
- **Know what's connected.** The status pill shows ONLINE/OFFLINE, which
  `jarvis` invocation is linked, and the exact config path in use.
  Reconnects automatically with backoff if the server restarts; click the
  pill to retry immediately while offline.
- **Ask Jarvis.** The **Ask Jarvis** button opens a full chat panel wired
  to the CLI's own AI mode (`jarvis "<text>"`) — same persona, same
  multi-provider (and multi-key) failover, same conversation memory, just
  with a proper thread instead of one line at a time. Provider-by-provider
  trace (which one's being tried \u2014 including which key, when a provider
  has more than one \u2014 and why an earlier attempt failed) streams in live
  above the reply, so failover isn't a black box. **Clear** wipes that
  memory (calls `jarvis ai-clear`) and starts the thread over.
  See [jarvis-cli/README.md](../jarvis-cli/README.md#talking-to-jarvis-ai-mode)
  for the config format, provider list, and how failover decides what
  counts as "failed".
- **AI Config.** The **AI Config** button opens `ai_config.json` the same
  way **{ } Config** opens `commands.json` — persona, defaults, and every
  provider, including its `api_keys` list, in one raw-JSON editor. Add a
  second (or third) key to any provider's array right from the browser;
  it's read fresh on the very next ask, nothing to restart.

Nothing here is faked or mocked — every action above goes through the
real CLI or the real config file on disk.

## How it talks to jarvis

Two separate paths, matching the CLI's own two jobs:

1. **Structured edits** (list, create, update, delete a command) read and
   write `commands.json` directly as JSON — the same file `jarvis config`
   points you to.
2. **Running a command** spawns the actual `jarvis` process with the
   same argv you'd type yourself (`jarvis name --flag value`, or
   `name1 then name2 ...` for a sequence) and streams its stdout/stderr
   back over the WebSocket as it's produced. Execution logic — condition
   matching, step order, exit codes — is never reimplemented here;
   whatever the CLI does is exactly what runs.
3. **Asking Jarvis** spawns the exact same `jarvis` process, just with
   your free-text message as a single argv element instead of a command
   name — precisely what `jarvis "<text>"` does at a terminal — and
   streams the reply back the same way. All of the AI logic (which
   provider to try, persona, conversation memory, failover) lives in the
   CLI's Python; this server has no idea any of that exists, it just
   passes text in and streams text back out.

On startup the server looks for a working `jarvis` in this order: `jarvis`
on PATH, then `python3 -m jarvis`, `python -m jarvis`, `py -m jarvis`. It
confirms each candidate by actually running `<candidate> config` and
checking it prints a path. Override this with an environment variable if
your setup needs something specific:

```bash
JARVIS_BIN="python3 -m jarvis" npm start
```

## Security note

The server binds to `127.0.0.1` only and has no authentication — by
design, for a tool that runs real shell commands from your own
`commands.json` on your own machine. Don't put this behind a reverse
proxy or otherwise expose it beyond localhost without adding your own
auth layer first; anyone who can reach it can run anything your
`commands.json` can run.

## Configuration

| Variable     | Default        | Purpose                                                                 |
|--------------|----------------|--------------------------------------------------------------------------|
| `PORT`       | `4173`         | Port the console listens on.                                             |
| `JARVIS_BIN` | auto-detected  | Exact invocation to run jarvis with, e.g. `"python3 -m jarvis"`. Space-separated command + args. |

```bash
PORT=8080 JARVIS_BIN="python -m jarvis" npm start
```

## REST API

All endpoints are JSON. Anything that needs jarvis returns `503` with an
explanatory `error` if the CLI isn't currently linked.

| Method & path              | Does |
|-----------------------------|------|
| `GET /api/status`           | `{ online, invocation, configPath }` |
| `POST /api/reconnect`       | Re-runs binary detection, returns the same shape as `/api/status` |
| `GET /api/commands`         | All commands, as `{ name: spec, ... }` |
| `GET /api/commands/:name`   | One command's spec |
| `POST /api/commands`        | Create — body `{ name, spec }` |
| `PUT /api/commands/:name`   | Update (and optionally rename) — body `{ name, spec }` |
| `DELETE /api/commands/:name`| Delete |
| `GET /api/raw`              | `{ text, path }` — the entire config file as text |
| `PUT /api/raw`              | Overwrite the entire file — body `{ text }`. Must be valid JSON with a top-level `commands` object; jarvis itself is the judge of anything inside it beyond that. |
| `POST /api/ai/clear`        | Wipe AI conversation memory (runs `jarvis ai-clear`) |
| `GET /api/ai/raw`           | `{ text, path }` — the entire `ai_config.json` as text |
| `PUT /api/ai/raw`           | Overwrite `ai_config.json` — body `{ text }`. Must be valid JSON, with `providers` (if present) as an array; jarvis itself is the judge of anything else inside it. |

A `spec` is exactly the CLI's own command shape — see
[`jarvis-cli/README.md`](../jarvis-cli/README.md#adding-commands) for the
full rules on `run`, `vars`, `if`/`unless`, `continueOnError`, `parallel`,
and `showCommand`.

## WebSocket protocol (`/ws`)

Send:
```jsonc
{ "type": "run", "segments": [{ "name": "deploy", "flags": { "env": "prod" } }] }
// multiple segments = a then/and chain, run in order \u2014 give segment i>0
// a "mode": "and" to run it alongside the previous segment instead of
// waiting for it (same then/and relationship as the CLI's own chain
// syntax); anything else (including an absent mode) means "then"
{ "type": "ask", "text": "what's a good way to organize my downloads folder" }
// free-text ask, routed to the AI \u2014 exactly like `jarvis "<text>"` on the CLI
{ "type": "cancel" }
// terminates whatever's currently running on this connection (a run OR an ask)
```

Receive, for `run`:
```jsonc
{ "type": "start",  "cmdline": "jarvis deploy --env prod", "count": 1 }
{ "type": "stdout", "line": "..." }
{ "type": "stderr", "line": "..." }
{ "type": "exit",   "code": 0, "signal": null }
{ "type": "error",  "message": "..." }
```

Receive, for `ask` — same shape, `ask-` prefixed so a client can tell the
two flows apart without inspecting content:
```jsonc
{ "type": "ask-start",  "cmdline": "jarvis <your message>" }
{ "type": "ask-stdout", "line": "J.A.R.V.I.S: ..." }  // the actual reply
{ "type": "ask-stderr", "line": "\u21b3 asking openai\u2026" }    // live failover trace
{ "type": "ask-exit",   "code": 0, "signal": null }
{ "type": "ask-error",  "message": "..." }
```

Only one thing runs at a time **per connection**, run or ask alike — a
second `run` or `ask` while one is already active gets back an
`error`/`ask-error` rather than queueing. `cancel` works on whichever
kind is currently active.

## Project structure

```
web/
├── server.js         Express + ws backend — the only thing that talks to jarvis
├── package.json
└── public/            Served as static files (this is the entire client)
    ├── index.html
    ├── app.js          All client logic, no build step, no framework
    └── style.css
```

No bundler, no framework, no build step — `public/` is served as-is.
Editing any of the three front-end files and refreshing the browser is
the entire dev loop. `npm run dev` restarts the server automatically on
save (uses Node's built-in `--watch`, which needs Node 18.11+).

## Troubleshooting

**Status pill says OFFLINE.** The server couldn't find a working `jarvis`.
Confirm `jarvis` (or `jarvis config`) works in a plain terminal first — if
it doesn't, the console won't find it either. Once fixed, click the
status pill, or restart `npm start`.

**Port 4173 already in use.** `PORT=4200 npm start`, then open that port
instead.

**Edited `commands.json` externally and the list looks stale.** The
console reads the file fresh on every request, so this shouldn't happen
in normal use — but if you edited it directly and don't see the change,
click **{ } Config** to reload straight from disk, or reload the page.

**A command hangs and Abort doesn't seem to do anything.** Abort kills
the whole process tree jarvis spawned for that run, not just a single
process — if something is blocking on its own I/O in a way that ignores
`SIGTERM`, it can still take a moment. It will not hang the console
itself; you can start a new run once the exit message arrives.

**Ask Jarvis says it has no AI providers configured / everything fails.**
That's the CLI talking, not the web console — `jarvis ai-config` (in a
terminal) shows you the file, and
[jarvis-cli/README.md](../jarvis-cli/README.md#talking-to-jarvis-ai-mode)
has the full setup and failover reference. If you just added a key and
nothing changed, remember the file's read fresh on every ask, so there's
nothing to restart here either.
