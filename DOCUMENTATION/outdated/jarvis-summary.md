# Jarvis — project summary

Jarvis is a personal AI agent written in Python, with a Node/Express web frontend
and a Playnite (game launcher) bridge. This document is a technical orientation
for another AI picking up work on the codebase.

## High-level architecture

- **`jarvis-cli/jarvis/`** — the Python package. This is the core: a CLI entry
  point (`cli.py`) that resolves a user's text prompt, builds a system prompt
  and tool catalog, and calls out to one of several LLM providers, with
  multi-provider/multi-key failover.
- **`web/`** — a Node/Express server (`server.js`) + vanilla JS/HTML/CSS
  frontend (`public/app.js`, `index.html`, `style.css`) that shells out to the
  `jarvis` CLI as a child process (WebSocket-streamed for `ask`, one-shot
  `spawn` for everything else) and renders a chat UI, a tool debug dashboard,
  and (as of this session) a skills manager.
- **`playnitebridge/`** — a bridge to the Playnite game-library manager,
  exposed to the AI as ~31 tools (`playnite_*`).

**Critical fact about the runtime model:** Jarvis is a brand-new OS process on
every single `jarvis ...` invocation. There is no long-running server process
for the CLI itself — no in-memory session, no warm cache in RAM. Anything that
needs to persist across calls (conversation history, tool-routing stickiness,
discovery caches, prompt-cache bookkeeping) is written to small JSON files
under `~/.jarvis/`. This constraint shapes a lot of design decisions described
below — most importantly, it's why "make the model re-request a tool's schema
if it needs it" is expensive here (a full resend of the entire prompt) in a
way it wouldn't be in a persistent-session agent.

## Core request flow (`ai_client.py`, ~2300 lines)

1. `cli.py` calls `ai_client.ask(user_text, ...)`.
2. `tool_router.route(user_text)` does local (non-AI) keyword matching against
   `tool_registry.TOOL_GROUPS` (a dict of group-name → list of tool names,
   e.g. `"playnite"`, `"desktop"`, `"spotify"`, `"skills"`, `"discovery"`) to
   guess which tool group(s) this message needs, without a model round trip.
   Returns a `route` object with `.confident`, `.groups`, `.tools`,
   `.matches` (which keyword hit which specific tool).
3. `route_stickiness.py` keeps a route's matched group "sticky" for a couple
   of follow-up turns per conversation, so an ambiguous follow-up ("do it")
   still gets the right tools offered.
4. Based on the route, `ai_client.ask()` builds `active_schemas` — the tool
   schemas that will actually be offered this round — starting from either
   the routed group, the sticky group, or (if the router has no opinion) just
   a tiny `search_tools`/`search_commands` discovery pair.
5. `_build_messages()` / `_system_prompt_parts()` assembles the system prompt
   (see "System prompt / prompt caching" below) and full message list.
6. `_make_tool_executor()` wraps tool execution, wiring in confirm-gating
   (`tool_safety.py`), a shared `RoundBudget` across provider failover, and a
   `discover_sink` callback that grows `active_schemas` in-place mid-ask when
   `search_tools`/`get_tool_schema` find a new match — so a discovered tool
   is *actually callable* on the model's next round, not just described.
7. `ask()` loops over `providers` (in `provider_priority` order from config),
   and within each provider, over its configured API keys, calling the
   matching adapter in `ai_providers.py`. First success wins. Each adapter
   internally loops up to `MAX_TOOL_ROUNDS` tool-call rounds.

## Providers (`ai_providers.py`)

One adapter function per provider *type* (not per named provider — multiple
named providers can share a type):

- `call_anthropic` — Anthropic Messages API
- `call_openai_compatible` — covers OpenAI, Groq, xAI, Mistral, DeepSeek,
  OpenRouter (anything with an OpenAI-shaped chat-completions endpoint)
- `call_gemini` — Google Gemini
- `call_cohere` — Cohere
- `call_ollama` — local Ollama

`token_usage.py` normalizes each provider's usage-reporting shape into a
common `{input_tokens, output_tokens, ...}` dict via `extract_usage()`.

## Tool system

- **`tools.py`** — the central tool catalog: `TOOL_SCHEMAS` (full list),
  `TOOLS` (name → handler dict), plus several *transformation* functions that
  take a schema list and return a cheaper representation:
  - `compact_schemas_for_prompt()` — strips verbose descriptions
  - `name_only_schemas_for_prompt()` — name + stub params only (no real
    schema)
  - `catalog_schemas_for_prompt()` — name + one-line summary, no params at
    all (the "tier 1" representation, see Hybrid catalog tier below)
  - `schemas_for_tools(names)` — full schemas for a specific name list
- **`tool_registry.py`** — `TOOL_GROUPS` (name → tool list),
  `TOOL_KEYWORDS` (router keyword weights), `TOOL_PACK_INSTRUCTIONS`
  (per-group workflow guidance text), `group_of(name)`, `tools_in_group()`.
- **`tool_loader.py`** — auto-discovers `jarvis/actions/*.py` files at import
  time; each exports `TOOL_SCHEMAS`/`TOOLS` and gets merged into the main
  catalog under its own router group. This is the extension point for adding
  new *capabilities* (things that run code) — e.g. `actions/dev_agent.py`,
  `actions/code_agent.py`.
- **`tool_safety.py`** — `DEFAULT_CONFIRM_REQUIRED`, a set of tool names that
  must be gated behind `on_confirm_request` (destructive actions).
- **`search_tools`** / **`get_tool_schema`** — a hand-built two-tier
  discovery mechanism, functionally equivalent to Anthropic's "Tool Search"
  / MCP lazy-loading pattern: `search_tools(query)` finds tool names by
  keyword without paying for their full schemas up front; `get_tool_schema
  (name)` fetches one tool's full argument schema on demand. Both feed into
  the `discover_sink` mechanism above so a match becomes *callable*, not just
  described.

### Hybrid catalog tier (in `ai_client.ask()`)

A router-matched group can be large — `playnite` is 31 tools (~2,700
compacted tokens), `desktop` 16, `system_control` 11. Sending the *entire*
group's schemas to call one tool is wasteful. The fix (`CATALOG_TIER_MIN_TOOLS
= 10`): when a route is confident and the matched group has ≥10 tools, split
it using `route.matches` (which records which tool each matched keyword
actually hit) into:
- **hot** tools (the ones the keywords plausibly meant) → full schema, fully
  callable immediately, no extra round trip
- **cold** tools (the rest of the group) → a `catalog_schemas_for_prompt()`
  stub (name + one-liner, no params) plus `get_tool_schema` to promote one on
  demand

This was deliberately *not* implemented as "stub everything, let the model
re-request" (the usual MCP-style lazy pattern) — because Jarvis is a fresh
process per call, a re-request costs a *full resend of the entire prompt*,
not a cheap in-session lookup. The hybrid split avoids that cost for the
common case (the tool the user meant) while still capturing the savings on
the long tail.

Precise mode (150% capacity, see below) opts out of this tier entirely via
`profile["catalog_tier"] = False`, since its whole purpose is maximum schema
fidelity over token savings.

**Known gotcha (real bug, since fixed):** the catalog-tier decision has to
happen *before* the per-provider retry loop in `ask()` (schemas are built
once and shared across every provider/key attempt), but `profile` — which
holds the `catalog_tier` flag — is normally resolved *inside* that loop
(because prompt mode can differ per provider). The fix resolves a
representative profile from the first eligible provider before the loop,
mirroring the same pattern `current_mode()` uses elsewhere in the file. If
you see `UnboundLocalError: cannot access local variable 'profile'` anywhere
near this code, this is almost certainly the shape of the bug.

## Capacity / prompt modes (`_MODE_BY_NAME`, `_prompt_profile()`)

Jarvis has a "capacity" concept controlling how much text goes into the
prompt — roughly `full` / `compact` (default) / `precise` (150%, max
fidelity) / `ultra` (50%, name-only schemas). Each mode is a profile dict of
knobs: `compact_tools_blurb`, `compact_persona`, `tool_schema_style`
(`"raw"`/`"compact"`/`"name_only"`), `catalog_tier`, history limits, etc.
Resolved per-provider via `_resolve_prompt_mode()` / `_prompt_profile()`,
since a legacy per-key override system (`_LEGACY_OVERRIDE_KEYS`) lets
individual providers deviate from the global default.

## System prompt / prompt caching

`_system_prompt_parts()` (called by `_build_messages()`) returns a
`(static_prefix, per_request_tail)` tuple instead of one joined string. This
split is the load-bearing piece of prompt caching support:

- **Static half:** persona, history nudge, tools blurb, the skills catalog
  (tier 1, see below), the precision directive. Identical across every turn
  that lands on the same capacity mode.
- **Dynamic half:** memory context (keyed on the user's message — different
  every turn), other-conversations context, per-turn `pack_instructions_ctx`
  (varies with which router group matched *this* turn), manually-loaded
  skill bodies (see skill_stickiness below — stable within one conversation,
  but not across different ones), Playnite frequent-games, saved-commands
  listing, frequency stats.

Every provider that supports prompt caching caches a byte-*prefix* of the
request. Anything query-dependent sitting in front of static content breaks
the cache on every turn. (There was a real bug here too: `pack_instructions_
ctx` used to live in the static half despite varying per-turn — since a
marked content block caches as a whole, any byte difference inside it misses
the *entire* block, not just the changed part. Fixed by moving it to the
dynamic half.)

`_system_prompt()` is kept as a thin wrapper joining the two halves, so
non-caching callers/tests see identical behavior to before the split.

### `prompt_cache.py` — the per-provider caching policy module

One module owns all caching decisions, because the failure mode is silent (a
prefix that never matches just returns a correct answer at full price, no
error). Key function: `plan(system_parts, tools_payload, provider, defaults,
model)` → a dict describing what to do, with a human-readable `reason`
string logged once per attempt (`_log_cache_plan` in `ai_providers.py`).

Per-provider behavior:
- **Anthropic** — explicit `cache_control: {"type": "ephemeral"}` markers.
  Caching is cumulative in request order (tools → system → messages), so a
  breakpoint on the system block caches the tool schemas too. Per-model
  minimum-cacheable-token floors are tracked in `ANTHROPIC_MIN_TOKENS` (e.g.
  `claude-haiku-4-5` needs ≥4096 tokens in the marked prefix — the bare
  system prompt alone, at 160–830 tokens, never clears this; it only works
  because the tool schemas ride along in the same block). A second
  breakpoint on the tools array is opt-in (`prompt_cache_tools`, default
  False) since a write that's never read costs a 1.25x premium and the tool
  list is the part most likely to change turn to turn.
- **Gemini** — defaults to *implicit* caching (free, automatic on 2.5+
  models, needs a stable prefix and nothing else). *Explicit* caching
  (`cachedContents` API) is opt-in (`gemini_explicit_cache`) and, when used,
  persists the cache name across processes via `gemini_cache_store.py`
  (fingerprinted by model+system+tools) so the write is actually amortized —
  the original implementation created a cache per-ask and deleted it at the
  end of that same ask, which was a near-guaranteed net loss for
  single-round asks.
- **OpenAI-family / Groq** — caches automatically above ~1024 tokens, no
  opt-in needed. The only lever is `prompt_cache_key`, a stable
  per-conversation string sent as a routing hint (default on).
- **Ollama** — no billed cache; the lever is `keep_alive` (default `"30m"`),
  since Ollama drops its KV-prefix reuse when it unloads the model after 5
  minutes idle by default — a real problem for a fresh-process-per-call
  agent with gaps between invocations.

Config keys live under `defaults` in `ai_config.json`, overridable per
provider: `prompt_cache`, `prompt_cache_ttl`, `prompt_cache_tools`,
`prompt_cache_key`, `gemini_explicit_cache`, `ollama_keep_alive`.

`token_usage.extract_usage()` surfaces `cache_read_tokens`/
`cache_write_tokens` separately per provider (Anthropic's
`cache_*_input_tokens`, Gemini's `cachedContentTokenCount`, OpenAI-family's
`prompt_tokens_details.cached_tokens`) rather than folding them silently into
the input total, specifically so a cache that never hits is diagnosable.

## Skills system (`skills.py`, `skill_tools.py`, `skill_stickiness.py`)

A **skill** is a folder under `~/.jarvis/skills/<slug>/` containing a
`SKILL.md` (YAML frontmatter + markdown body) plus optional `references/`
and `scripts/` subfolders. This is Anthropic's Agent Skills open standard —
also adopted by OpenAI/Copilot/Cursor, so skills are portable.

Three-tier progressive disclosure (mirrors the tool catalog's compact/
name-only/catalog split, applied to *knowledge* instead of *capabilities*):

1. **Tier 1 (discovery)** — `catalog_text()` renders one line per installed
   skill (name + description only) into the **static** half of the system
   prompt. Zero-cost when no skills are installed; ~20 tokens/skill
   otherwise, and — being static — amortized by prompt caching.
2. **Tier 2 (activation)** — `load_skill(name)` tool pulls the full SKILL.md
   body into context, only when the model decides a description matches the
   task. Capped at ~5k tokens (`BODY_SOFT_LIMIT`) with truncation + warning
   rather than outright rejection.
3. **Tier 3 (execution)** — `load_skill_reference(name, file)` reads one
   reference document at a time. **Scripts are refused at read time and
   pointed at how to run them instead** (`SCRIPT_SUFFIXES` check in
   `read_reference()`) — this refusal is the entire reason a skill can ship
   real code for ~0 ongoing token cost; without it, "reference" and "script"
   collapse into the same thing. Path containment is enforced by resolving
   both sides and checking containment (not string-matching `..`) — a
   relative path that legitimately resolves back inside its own folder is
   correctly allowed.

**Creating skills, including complex/multi-module ones:** `create_skill`
accepts optional `references={filename: content}` and `scripts={filename:
content}` dicts, written into `references/` and `scripts/` subfolders
(validated against a safe-filename pattern — no paths, no traversal). The
tool's description instructs the model: for a complicated skill, keep
`instructions` a short overview and split real detail across multiple
reference files rather than one giant `SKILL.md` — the model decides when a
skill is "complex enough" to split, since only it understands the semantic
boundaries; Jarvis just provides the mechanism.

**Importing existing skills:** `add_skill(source)` accepts a folder path, a
`SKILL.md` file path, a `.zip` archive path, or raw pasted markdown. Zip
import (`_install_from_zip`) handles both the "SKILL.md at the zip root" and
"SKILL.md one level down inside the zip's one folder" shapes, filters OS
junk (`.DS_Store`, `__MACOSX`), and defends against zip bombs (compressed-
size cap, uncompressed-size running total, entry-count cap) and zip-slip
(per-entry path containment check *before* extraction, into a throwaway temp
dir first — nothing lands in `~/.jarvis/skills` until the whole archive
passes every check).

**Manual loading** (`skill_stickiness.py`) — separate from the model's own
on-demand `load_skill` tool. Forces a skill's full instructions into *every*
ask for a scope — one conversation, or globally (every conversation) if no
conversation id is given — until explicitly unloaded. One JSON file,
`{scope: [skill_names]}`, where scope is a conversation id or the literal
`"*"`. `get_loaded()` unions conversation-specific + global. Self-healing:
if a loaded skill is later deleted, `loaded_context()` silently drops it from
the rendered prompt and cleans the stale entry out of storage rather than
surfacing an error on every subsequent ask.

Two ways to trigger manual loading:
- **CLI:** `jarvis skillload <name> [conversation-id]`,
  `jarvis skillunload <name>|--all [conversation-id]`, plus `skillmake`/
  `skilladd` as friendlier aliases for `skills-create`/`skills-add`.
- **Chat:** `/skillload <name>` / `/skillunload <name>` typed directly into
  the Ask box — intercepted client-side in `app.js` before ever reaching the
  model (same pattern as the existing `ORGANIZE_JSON_RE` local-command
  interception), with autocomplete suggesting matching skill names as you
  type. `/skillmake` and `/skilladd` just open the Skills manager to the
  right pane, since composing a whole skill on one chat line isn't
  practical.

Six AI-facing tools in `skill_tools.py` (`skills` router group):
`list_skills`, `load_skill`, `load_skill_reference`, `create_skill`,
`add_skill`, `remove_skill` — `remove_skill` is confirm-gated
(`tool_safety.py`). Six CLI commands mirror them 1:1 (`skills-list`,
`skills-get`, `skills-save`, `skills-add`, `skills-create`, `skills-remove`),
plus REST endpoints under `/api/skills/*` in `server.js` that proxy the CLI
(chosen over `/api/tools/run` deliberately: the web manager is a person
editing their own files and must not inherit the AI-facing confirm gate, and
raw-file read/write on `SKILL.md` is intentionally *not* exposed as a model
tool at all).

## Web frontend (`web/`)

- `server.js` — Express server; shells out to the `jarvis` CLI. Ask requests
  go over WebSocket (streamed stdout); most other actions (tools-list,
  skills-*, etc.) are one-shot `spawn` calls parsed as JSON. `requireJarvis`
  middleware gates every endpoint on the CLI being resolvable.
- `public/app.js` (~6,400 lines) — single-file vanilla-JS frontend. Notable
  subsystems: the Ask panel + WebSocket streaming, a Debug dashboard (lists
  every tool, lets you run one directly with a confirm/AI-review toggle), and
  (new) a Skills manager overlay (list/editor/create/import panes, a live
  token-cost readout for the catalog, zip upload via a dedicated raw-body
  endpoint).
- `public/index.html` / `style.css` — markup/styling to match.

## Testing

`tests/*.py` are standalone scripts (no pytest — each defines its own
`check()`/pass-fail accumulator and calls `sys.exit(1)` on any failure), run
individually from `jarvis-cli/`: `python3 ../tests/test_X.py`. Notable files
added this session: `test_prompt_cache.py`, `test_skills.py`,
`test_skill_stickiness.py`. Two pre-existing files
(`test_dev_agent_events.py`, `test_enhancements.py`) and part of a third
(`test_provider_override.py`) fail on the *original* unmodified repo too —
confirmed by running them against a pristine extraction — so they're known,
unrelated pre-existing issues, not regressions from this work.

## Config

`ai_config.py` / `~/.jarvis/ai_config.json` — `providers` (list of named
provider configs: type, base_url, api_keys, model) and `defaults` (global
knobs: `provider_priority`, `prompt_mode`, `tools_enabled`, timeout, and all
the `prompt_cache*`/`ollama_keep_alive` keys above). Per-provider blocks can
override most `defaults` keys.

## Reference docs in the repo

- `TOKEN-OPTIMIZATION-AND-SKILLS.md` (repo root) — detailed writeup of the
  caching + hybrid-catalog + skills work, with before/after token
  measurements.
- `skills-and-token-optimization-research.md` — the original research doc
  this work was implementing (Anthropic Agent Skills, MCP lazy tool loading,
  prompt caching, as of ~Sept 2026).
- `DOCUMENTATION/` — pre-existing project docs (`jarvis_everything_i_know.md`,
  `PROJECT_SUMMARY.md`, yt-dlp/everything-SDK references for other tool
  groups).

---

## Scheduling, notifications, reminders, conversation search, MCP (added this session)

Full writeup: `SCHEDULING-NOTIFICATIONS-AND-MCP.md` in the repo root. The
short orientation:

### One scheduling engine, three faces

`scheduler.py` holds one record type — a JOB — with two independent halves:
a **trigger** (`at` / `every` / `event` / `startup`) and an **action**
(`notify` / `ask` / `command` / `tool`). `kind` (`task`/`notify`/`reminder`)
is purely labelling; a reminder IS a notify-action job. That's why reminders
"use the notifying engine" — there was never a second one, and it's what
makes chaining possible ("when the backup task is done, notify me").

**The constraint that shapes everything:** jarvis is a fresh process per
invocation, so nothing can wake up at 9am on its own. There is no daemon and
this module doesn't pretend otherwise. Firing is driven by an explicit
`jarvis sched-tick`, called by web/server.js on a 30s interval (plus one
`--startup` tick on boot), or by Task Scheduler/cron, or manually. A lock
file makes concurrent ticks a no-op rather than a double-fire. `jarvis ask`
*drains* notifications but never fires jobs — running a 300s command on the
latency path of every reply would be unacceptable.

`notifier.py` splits delivery into a **durable inbox** (always written,
per-consumer acknowledgement so the web console and a terminal each see it)
and best-effort live channels (`stream`/`toast`/`voice`/`playnite`). A
reminder firing into a closed browser must still be waiting later.

`timespec.py` parses "every weekday at 08:30" deterministically with no new
dependencies, and **raises rather than guessing** — a reminder at the wrong
hour is worse than one that was never created.

Anything that RUNS unattended (a command, a gated tool, a full ask) is
created at `status="needs_approval"` and needs `jarvis sched-approve <id>` or
the web panel. The model cannot approve its own job — same principle as not
letting it fill in `confirm: true`.

Six tools in `actions/scheduler_tools.py` (group `scheduling`):
`remind_me`, `notify_me`, `schedule_task`, `list_scheduled`,
`cancel_scheduled`, `signal_event`.

### Conversation search (`conv_search.py`)

A grep over the flat conversation JSON, not an embedding index — no model
call, no background job, nothing that can silently lag behind the files it
describes. Modes: `words` (all terms, any order), `phrase`, `regex`. Tool
traffic is searchable but opt-in. Exposed as `search_conversations`, joined
to the **memory** group, and as `jarvis conv-search`. It's the deliberate
on-request exception to `other_conversations_context()`'s titles-only rule.

### MCP client (`mcp_client.py` + `actions/mcp_tools.py`)

Speaks JSON-RPC to external MCP servers over stdio or HTTP. The
fresh-process problem again: connecting to every server at import time would
add seconds to every ask, so **discovery reads a disk cache** and only
*calls* connect for real (pooled per process).

The neat part: `actions/mcp_tools.py` BUILDS its `TOOL_SCHEMAS` from that
cache at import time, so a foreign protocol plugs into the catalog with zero
changes to `tools.py`/`tool_loader.py`/`tool_safety.py`. Tools are named
`mcp_<server>_<tool>` and are **confirm-gated unless the server is marked
trusted**. Servers can only be added by editing `mcp_config.json` — never by
a model tool.

Caveat: new tools need a jarvis restart after `mcp-refresh`, since discovery
runs at import time.

### Conversation-persistence fixes

Three separate bugs that all looked like "my conversation didn't save":

1. **Persistence only happened on success** — `append_exchange` lived inside
   `if result.ok:`, so the web Stop button (which kills the child outright)
   discarded the user's message entirely. Now `begin_exchange()` writes the
   user's half *before* any provider is contacted, and
   `complete_exchange()`/`abandon_exchange()` resolve it. A SIGTERM/SIGINT
   handler in cli.py records *why* a turn died. `conversation_messages()`
   filters unanswered turns so an empty assistant message never reaches a
   provider (a hard 400 on Anthropic).
2. **`present_file` was the only media tool missing from
   `_extras_from_runs`**, so its card lived only in the live JARVIS_MEDIA
   stream and vanished on reload. Fixed on both sides; the extra's field
   names match `showAskPresentFile(info)` so replay uses the same renderer.
3. **Work done before an abort was discarded** — `abandon_exchange()` now
   stores `extras` too.

### Tests

`test_timespec.py` (50), `test_scheduler.py` (62),
`test_conversation_persistence.py` (34), `test_mcp_client.py` (33),
`test_conv_search.py` (30) — 209 total, same standalone no-pytest convention.
`test_mcp_client.py` spawns a real, deliberately badly-behaved stdio server.
The pre-existing failures noted above (`test_dev_agent_events`,
`test_enhancements`, part of `test_provider_override`) are unchanged.
