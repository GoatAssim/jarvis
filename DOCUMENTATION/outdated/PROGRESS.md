# Jarvis → AI Jarvis — progress tracker

You asked for a big set of features, on purpose split into steps so each
one lands as a working, testable delivery instead of one giant untested
blob. This file tracks what's done, what's next, and exactly what
changed at each step. Updated at the end of every step.

## The full request, broken into steps

1. **The AI brain.** `jarvis "<text>"` talks to an AI, in character as
   Jarvis, with support for many providers/API keys configured at once
   and automatic failover between them (on error *or* on refusal) — and
   the same thing available from the web UI. **Done**, plus: more than
   one key per provider, parallel-or-sequential execution (steps and
   chained commands), a per-step command-visibility toggle, and real
   tool-calling — Jarvis can now check battery/Wi-Fi/location/date-time/
   disk/memory/system info for real, live answers instead of guessing.
   ← **you are here**
2. **Hands.** Jarvis can actually run one of your existing commands from
   a natural-language request ("jarvis update my spotify"), can draft
   *new* commands for you from a plain-English description (opening the
   web UI's existing interactive command builder, pre-filled, for you to
   review before saving), and is honest — "I don't have a function for
   that yet, sir" — when it genuinely can't do something. The tool-
   calling machinery this needs already exists now (see below) — running
   a command becomes one more tool alongside the system-info ones.
   *Planned next.*
3. **Voice.** Text-to-speech for Jarvis's replies in the web UI (both
   for the Ask Jarvis panel and command results), voiced to sound like
   the AI-butler archetype, running locally where possible. *Planned —
   not started yet.*
4. **Polish.** True token-by-token streaming in the chat UI, a couple of
   quality-of-life additions that fall out of using steps 1–3 for real.
   *Planned, scope depends on what step 2–3 actually need once built.*

---

## Step 1 — The AI brain ✅ done

### Hotfix (after your first real test with a live key)

Real Groq responses crashed the whole program on Windows:
```
UnicodeEncodeError: 'charmap' codec can't encode character '\u202f'
```
**Why:** a real AI reply can contain almost any Unicode character (that
one's a narrow no-break space, U+202F — models use these constantly in
things like "9 : 00 AM" spacing). Windows consoles default to a legacy
codepage (`cp1252` here) that only understands a small, fixed character
set, and printing anything outside it crashed the process. This never
showed up in step 1's own testing because that used a local mock server
and hand-written test strings, not a real model's actual output — exactly
the kind of gap real testing catches.

**Fix** (`cli.py`, top of file): stdout/stderr are now explicitly set to
UTF-8 with `errors="replace"` before colorama wraps them, so any
character Windows' legacy codepage can't represent gets swapped for a
safe placeholder instead of crashing the program. This runs once, at
startup, ahead of every other print in the program, so it isn't specific
to AI replies — it's a blanket fix. Confirmed against a mock server
returning a real reply containing that exact character, under a
simulated `cp1252` console: crashed before the fix, clean exit 0 after.
Version bumped to 1.2.1. Reinstall with `pip install -e .` to pick it up.

### What you can do now

```
$ jarvis ai-config                                    # find/create the AI config file
$ jarvis "what's a good way to organize my downloads"  # talk to it
$ jarvis update my spotify                             # unquoted words work too (see note below)
$ jarvis ai-clear                                      # wipe its memory of you
```

...and the same thing in the web console: a new **Ask Jarvis** button
opens a full chat panel, styled to match the existing HUD theme, with
live failover trace shown as it happens.

> **Note on step 1 vs. step 2:** Jarvis can already see your commands
> (names + descriptions) and will mention them intelligently, but can't
> yet *run* them — `jarvis update my spotify` gets you a real, in-character
> reply acknowledging it can't trigger `updateSpotify` itself yet, not an
> actual Spotify update. That's step 2.

### Setup required before this does anything

The AI features need at least one real API key (or a local Ollama
install). Nothing works with the starter config as shipped — that's
intentional, it's your key/your choice which provider(s).

1. `cd jarvis-cli && pip install -e .` — **required even if you already
   had jarvis installed.** This step added a new dependency (`requests`)
   that a plain code update wouldn't pick up on its own.
2. `jarvis ai-config` → open the file it prints, paste an API key into
   any one provider block (OpenAI, Anthropic, Gemini, xAI, Mistral, Groq,
   DeepSeek, OpenRouter, or Cohere — or install Ollama locally and skip
   keys entirely).
3. `jarvis "hello"` → should get a real reply.
4. `cd web && npm start` (no new npm dependencies this step — your
   existing `node_modules` from before still works) → **Ask Jarvis**
   button, top right.

Full reference for the config format, the complete provider list, and
exactly how failover decides what counts as "failed":
**[jarvis-cli/README.md → Talking to Jarvis](jarvis-cli/README.md#talking-to-jarvis-ai-mode)**.

### What changed, file by file

**`jarvis-cli/jarvis/`** (Python)
| File | Change |
|---|---|
| `palette.py` | *New.* The color-output helper, split out of `cli.py` so `ai_client.py` can use it too without an import cycle. |
| `ai_config.py` | *New.* Loads/creates `~/.jarvis/ai_config.json` — persona, defaults, and the 10-provider starter list. |
| `ai_providers.py` | *New.* One adapter per provider "type" (`openai_compatible`, `anthropic`, `gemini`, `cohere`, `ollama`) — request shaping, response parsing, error/refusal detection. |
| `ai_client.py` | *New.* Orchestrates: builds the system prompt (persona + your commands + your most-used commands + a recap of recent conversation), tries providers in order, saves the exchange on success. |
| `history.py` | *New.* Rolling conversation memory on disk (`conversation_history.json`) — this is what gives Jarvis continuity between separate `jarvis` calls. |
| `stats.py` | *New.* Counts how often each command actually runs (`usage_stats.json`), for the "your most-used commands are..." context. |
| `cli.py` | *Edited.* New `ai-config` / `ai-clear` directives; anything that isn't a known command name now routes to `handle_ai_prompt()` instead of "Unknown command"; `run_command()` now bumps usage stats. Nothing about existing command execution changed. |
| `pyproject.toml`, `__init__.py` | Added the `requests` dependency; version bump to 1.2.0. |

**`web/`** (Node — `web v1/` untouched, it's your own backup copy)
| File | Change |
|---|---|
| `server.js` | *Edited.* New WebSocket `ask` message type (spawns `jarvis "<text>"`, streams the reply back as `ask-*` events — refactored the run-flow's spawn/stream code into a shared helper rather than duplicating it) and a new `POST /api/ai/clear` endpoint. No AI logic lives here; it only ever spawns the real CLI, same as it already did for running commands. |
| `public/index.html` | *Edited.* Added the "Ask Jarvis" button and the chat overlay panel markup. |
| `public/app.js` | *Edited.* Chat rendering, the WS `ask-*` message handling, open/close/clear/stop wiring. |
| `public/style.css` | *Edited.* New styles for the chat panel/bubbles, matching the existing design tokens (same colors, fonts, sharp-cornered HUD look) — no changes to any existing rule. |

Both `RESERVED_NAMES` lists (Python's and the web server's, which
validates command names before they hit the CLI) now include `ai-config`
and `ai-clear` alongside the pre-existing `config`/`then`/`-h`/`--help`,
so you can't accidentally create a command that'd be shadowed by one of
the new directives.

### How this was tested

No real API keys or internet access were available while building this,
so correctness was verified against a local mock server that returns
each provider's *actual* documented response shapes (including their
specific refusal/safety-block signals) — confirmed:
- Every adapter's success path parses correctly (OpenAI-compatible,
  Anthropic, Gemini, Cohere, Ollama).
- Failover triggers correctly on: HTTP 401, HTTP 429, HTTP 500,
  connection-refused, Anthropic's `stop_reason: "refusal"`, and Gemini's
  `promptFeedback.blockReason` — and stops at the first success without
  trying anything further down the list.
- A disabled provider, and an unfilled (`api_key: ""`) provider, are
  both correctly skipped rather than "tried."
- The full web stack (real `server.js` + real `jarvis` CLI + mock
  provider) end-to-end over an actual WebSocket connection, plus a
  regression pass confirming the pre-existing command-run flow still
  works unchanged after refactoring the connection handler.
- Persona, your defined commands, your most-used commands, and a recap
  of the previous exchange all showed up correctly in the actual request
  sent to the (mock) provider on a follow-up call.

What *hasn't* been tested (no way to, from here): an actual paid API key
against a provider's real, live servers. The request/response shapes for
OpenAI, Anthropic, and Gemini were checked against current docs while
building this; the OpenAI-compatible ones (xAI, Groq, Mistral, DeepSeek,
OpenRouter) inherit that shape by design. If one of them has since
changed something, the fix is isolated to that one adapter function in
`ai_providers.py` — it won't affect the other nine.

---

## Addendum — multi-key failover, parallel/sequential execution, show-command toggle ✅ done

Requested alongside step 2 but scoped as its own pass — your words were
"provider/failover plumbing," kept out of the same pass as step 2's
tool-calling so that stayed a clean, separate delivery. Three things,
all independent of the step 1–4 roadmap above:

1. **More than one API key per provider.** Every provider block can now
   hold `"api_keys": [...]` instead of (or alongside) a single
   `"api_key"`. Failover now happens key-by-key *within* a provider
   before moving to the next provider — same "log why, try the next
   one" behavior as step 1 shipped, just one level more granular. The
   web console's new **AI Config** button opens `ai_config.json`
   directly (same raw-JSON-editor pattern as **{ } Config** for
   `commands.json`), so adding a second key doesn't mean hunting for the
   file by hand.
2. **Parallel or sequential — per step, and per chained command.** A
   step can now be marked `"parallel": true` to start alongside
   whichever step(s) came right before it instead of waiting for them —
   steps naturally form "batches" this way, one batch waiting for the
   previous one to fully finish before it starts. The exact same
   relationship now exists one level up, between whole chained commands:
   `then` (wait, as before) and a new `and` (run together). The web
   builder's step cards got a matching checkbox (hidden on whichever
   card is first, since there's nothing before it to run alongside), and
   the sequence bar's queued items got a clickable → / ∥ connector
   between them.
3. **Per-step "show command" toggle.** `"showCommand": false` on a step
   suppresses just its `▶ name` / `$ the command` trace line before it
   runs — the step's own real output always still prints regardless, so
   this is purely about not cluttering the log with an already-obvious
   command (a spoken-style `echo` confirmation, say), never about hiding
   what actually happened. *(One judgment call worth flagging: your
   original note read as if the last step in a run should never show its
   command line even with the toggle on. Implemented instead as a plain,
   uniform per-step toggle with no special-casing — partly because your
   own example is consistent with that simpler reading too once you
   notice `echo`'s real stdout always prints regardless of the toggle,
   and partly because a toggle that silently does nothing on whichever
   step happens to run last seemed like a worse default. Says so on the
   step-card tooltip in the builder; flip it back to the other behavior
   in one message if you'd rather have it.)*

### What changed, file by file

**`jarvis-cli/jarvis/`** (Python)
| File | Change |
|---|---|
| `ai_config.py` | *Edited.* `api_keys` (a list) is now the primary field per provider in the starter template; new `provider_keys()` helper normalizes `api_keys` + the legacy singular `api_key` into one ordered, de-duplicated list — the one place anything else needs to look, so nothing downstream cares which form a given file happens to use. |
| `ai_client.py` | *Edited.* `ask()` now loops every key for a provider before moving to the next provider; attempt labels get a `(key i/N)` suffix once a provider has more than one key configured, so the live trace makes it obvious which specific key failed. |
| `cli.py` | *Edited.* Steps now execute in batches (`_group_into_batches` + `_run_batch`) instead of strictly one at a time — a `"parallel": true` step joins the previous step's batch and they run concurrently via `subprocess.Popen`, started together and waited on together. Chaining got the same treatment one level up: `split_chain_batches()` replaces `split_chain()`, understanding a new `and` separator alongside `then`; a batch of more than one chained command runs each on its own daemon thread (`_run_segment_batch`). `normalize_steps()` now also captures `parallel`/`showCommand`; `describe_steps()` (the `--help` epilog) notes both. `and` added to `RESERVED_NAMES`. |
| `stats.py` | *Edited.* `bump()` is now guarded by a lock — chained commands can genuinely run concurrently on separate threads within one process now, and two of them updating `usage_stats.json` at the same moment could otherwise silently lose an update. |
| `pyproject.toml`, `__init__.py` | Version bump to 1.3.0. |

**`web/`** (Node — `web v1/` and `jarvis-cli v2/` untouched, your own backup copies)
| File | Change |
|---|---|
| `server.js` | *Edited.* `buildArgv()` inserts `and` instead of `then` for a chain segment carrying `"mode": "and"`. `validateSpec()` type-checks the new `parallel`/`showCommand` step fields. New `GET`/`PUT /api/ai/raw`, mirroring the existing `/api/raw` pair but for `ai_config.json` (path discovered by running `jarvis ai-config`, same trick already used for the `ai-clear` endpoint). `and` added to the reserved-names set. |
| `public/index.html` | *Edited.* Step-card template got two new checkboxes (parallel, show command). New **AI Config** modal (same raw-JSON-editor markup as the existing config modal) plus a topbar button to open it. |
| `public/app.js` | *Edited.* Step editor reads/writes the two new fields and hides the "parallel" checkbox on whichever card is currently first (recomputed after add/remove/reorder/drag, so it always tracks the right one). Steps-preview shows a badge for a parallel or hidden-command step. Sequence bar tracks a `mode` per queued item and renders a clickable → / ∥ connector between chips, sent through to the server as part of each segment. AI Config modal open/reload/save wiring, mirroring the existing raw-config modal exactly. |
| `public/style.css` | *Edited.* Two new `.tag` modifiers (parallel, hidden) and a `.seq-connector` style, built from the same existing design tokens as everything else — no changes to any existing rule. |

### How this was tested

Still no real API keys or internet access available here, so:
- **Batching logic** (`_group_into_batches`, `split_chain_batches`,
  `provider_keys`) checked directly against hand-built inputs covering
  the edge cases: a leading step/segment marked parallel (must be
  ignored — nothing precedes it to run alongside), stray/duplicate
  separators, and key-list de-duplication against the legacy `api_key`
  field.
- **Real end-to-end timing**, using actual subprocesses under a
  temporary `$HOME` so nothing touched your real `~/.jarvis/`: a
  two-step sequential command took about twice as long as the same two
  steps with the second one marked parallel, confirming they genuinely
  overlap rather than just being silently reordered; the same check
  repeated one level up for a `then` vs. an `and` chain of whole
  commands, with matching results.
- **`showCommand`**, in that same real run: the toggled-off step's
  `$ echo ...` trace line was absent from stderr while its actual
  stdout output still printed correctly — confirming the toggle only
  ever touches the trace line, never the step's real output.
- **Failure propagation**: a failing step correctly stopped its chain
  and reported the right exit code without running what came after it,
  and `and` correctly got flagged as a reserved command name, matching
  `then`'s existing behavior.
- **Multi-key failover**, running the real `ask()` function against
  mocked provider adapters standing in for the network: confirmed
  key-by-key order within a provider before moving on, correct
  `(key i/N)` labels, and every individual key's failure reason
  recorded separately rather than only the last one.
- **Web server logic** (`buildArgv`, `validateSpec`, `validateName`):
  network access wasn't available to `npm install` express/ws here, so
  `node --check` confirmed `server.js` itself parses cleanly, and the
  exact changed functions were additionally exercised in isolation
  against both the new cases and the pre-existing ones (plain string
  `run`, existing condition validation) to guard against a regression.

Not tested (same reason as step 1 — no live network from here): an
actual provider's real HTTP error shape for a genuinely revoked or
rate-limited key, and a real browser click-through of the new AI Config
button/modal and the sequence bar's connector toggle. The logic
underneath both is the same raw-JSON-modal and WebSocket-message
plumbing step 1's Ask Jarvis panel and command builder already use
successfully — just pointed at a different file, or carrying one more
field.

---

## Tools — Jarvis can sense its environment ✅ done

You asked for the ability for the AI to make/execute tools, specifically
naming battery, location, wifi, time, and date. This is real function-
calling (every major provider's actual "tools" API, not a prompt trick)
plus seven built-in tools: the four/five you named, plus system info,
disk space, and memory — reasonable, safe, read-only additions in the
same spirit. This is also the general-purpose *infrastructure* step 2
("Hands") needs — running a jarvis command from natural language will
plug into this exact same mechanism as an eighth tool, rather than
needing its own separate system.

### What you can do now

```
$ jarvis "how's my battery"
  ↳ asking openai…
    ⚙ checking battery…
J.A.R.V.I.S: 74% and not plugged in, sir — you've got a while yet.

$ jarvis "what wifi am I on, and what time is it"
J.A.R.V.I.S: You're on "HomeNet-5G", and it's 9:42 PM on a Tuesday.
```

One ask can trigger several tools at once (as above) when the model
decides it needs more than one piece of information to answer — that's
provider behavior, not something jarvis has to orchestrate specially.

The seven tools: `get_battery`, `get_wifi_info`, `get_location`
(approximate, from your public IP — not GPS), `get_datetime`,
`get_system_info` (OS/version/hostname/uptime), `get_disk_usage`,
`get_memory_usage`. All read-only — none of them change a setting, write
a file, or run anything. That's deliberate: this is a fixed, small,
developer-defined toolkit, not the AI executing arbitrary code (a
different, more carefully-guarded question if you ever want it — running
one of *your own* predefined jarvis commands, in step 2, stays inside
the same safe envelope commands.json already has today, rather than
opening that door).

Jarvis decides on its own whether a question actually needs a tool —
"tell me a joke" doesn't trigger one, "what's my battery at" does. The
persona prompt draws an explicit line between this (real, working, right
now) and running your own jarvis commands (still can't, honestly says
so) so it doesn't get the two confused.

Turn it off entirely (e.g. to guarantee every ask is a single request,
no matter what) by setting `"tools_enabled": false` in `ai_config.json`'s
`defaults` block — same file, same "read fresh every time" behavior as
everything else.

### Setup required

1. `cd jarvis-cli && pip install -e .` again — **new dependency**
   (`psutil`, for battery/memory/uptime). Same reason as `requests` was
   needed in step 1: a code update alone doesn't add a package.
2. That's it — the other tools (wifi, location, datetime, disk, system
   info) use nothing beyond what's already installed. Nothing to
   configure; tools are on by default (`tools_enabled: true`).

### What changed, file by file

**`jarvis-cli/jarvis/`** (Python)
| File | Change |
|---|---|
| `tools.py` | *New.* The seven tool functions (each returns a plain dict, `{"error": "..."}` on failure — never raises), their provider-agnostic schemas, and `execute_tool(name)` to dispatch by name. |
| `ai_providers.py` | *Edited.* Every `call_*` adapter gained optional `tools=`/`tool_executor=` parameters and a small internal loop (max 4 follow-up rounds, then a clean "gave up" failure rather than looping forever): send the tools, check the response for a tool call, run it via `tool_executor`, feed the result back in *that provider's own wire format*, ask again. Each adapter still fully self-contained — the loop logic is duplicated per adapter on purpose (same reasoning as the rest of the file: a Gemini-shaped bug can't touch Anthropic). With no `tools` passed, behavior is byte-for-byte what step 1 shipped. |
| `ai_client.py` | *Edited.* `ask()` builds the tool list from `tools.py` and a `tool_executor` closure (wraps `tools.execute_tool`, firing a new `on_tool_call` callback first) when `defaults.tools_enabled` isn't false, and passes both down to whichever adapter/key it's currently trying. System prompt gained one new paragraph describing the tools *and* explicitly separating them from the still-honest "can't run your commands" line, so the two capabilities don't blur together for the model. |
| `ai_config.py` | *Edited.* `defaults.tools_enabled: true` added to the starter template. |
| `cli.py` | *Edited.* `handle_ai_prompt()` passes a new `on_tool_call` callback to `ask()`, printing a live `⚙ checking battery…`-style trace line to stderr — same convention as the existing `↳ asking openai…` line, and it shows up automatically in the web console's Ask Jarvis panel with zero web-side changes, since that trace already flows through as an `ask-stderr` line. |
| `pyproject.toml`, `__init__.py` | Added the `psutil` dependency; version bump to 1.4.0. |

**`web/`** — untouched this pass. The tool-call trace and every tool
result already flow through the existing `ask`/`ask-stderr`/`ask-stdout`
WebSocket messages from step 1 with no changes needed on this side.

### How this was tested

Real tool functions were run for real against this machine (battery,
system info, disk, memory, wifi — the last correctly reporting "not
determinable" here, same as any headless/no-adapter machine would) and
`get_location` against a mock geolocation endpoint covering both its
success and failure response shapes. For the tool-*calling* loop itself
— the part that talks to each AI provider — no live provider tool-
calling API was reachable from here either, so a second mock server was
built that's stateful enough to matter: it inspects each incoming
request and only returns a final answer once it can see a tool result
already in the conversation, otherwise it returns a tool call — meaning
the test genuinely exercises round 1 *and* round 2 of the real loop in
`ai_providers.py`, not just a canned single response. Confirmed against
that: every adapter's tool round-trip (OpenAI-compatible, Anthropic,
Gemini, Cohere, Ollama), a model asking for two tools in one round
(handled together, not as two separate round-trips), Ollama's
already-decoded-dict argument shape specifically (vs. everyone else's
JSON-string), Gemini's schema needing uppercase JSON-schema `type`
values (confirmed present in the actual request body, not just assumed),
a model that never stops calling tools (fails cleanly after 4 rounds
instead of hanging), and that omitting `tools` entirely reproduces
step 1's request byte-for-byte (no regression). Then the whole thing
again end-to-end: a real `jarvis` subprocess against the mock, and again
through the real `server.js` + WebSocket, confirming the trace line and
final reply both arrive correctly on that path too. Finally, the
complete step-1 regression suite (8 cases: success/refusal/rate-limit/
timeout/disabled-provider per adapter) was re-run unchanged against this
version and still passes, confirming this didn't disturb the non-tool
path.

Cohere is, as noted in `ai_providers.py`, the one adapter built without
a confirmed real example of the specific tool-*result* message shape
(request-scoped searches turned up the request format and the fact that
`tool_calls` comes back on the response, but not a documented example of
what to send back) — it's built to match its own already-established
OpenAI-ish shape as the best available inference, isolated to one
function, flagged in both the code comment and here so it's the first
place to look if Cohere specifically ever errors on a tool call.

---

## Step 2 — Hands (planned)

Real command execution from natural language, and AI-assisted command
creation via the existing interactive builder. The tool-calling
infrastructure both need is already built (see Tools, above) — running
a command becomes `execute_command(name, vars)` registered as one more
tool, reusing the exact same loop, trace output, and "only tries if the
model actually decides to call it" behavior every tool already has.
Command *creation* still needs its own design pass (what the AI is and
isn't allowed to put in a generated command, and the review step before
it's saved) since unlike the read-only tools above, that one writes to
`commands.json`.

## Step 3 — Voice (planned)

TTS in the web UI for both the Ask Jarvis panel and command results.

## Step 4 — Polish (planned)

Whatever step 1–3 leave on the table, plus real token streaming in the
chat UI.
