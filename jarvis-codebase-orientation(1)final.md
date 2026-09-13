# Jarvis Token-Optimization System — Codebase Orientation

Read this before grepping around. It's a map of what's already true in this
codebase, written by an AI that already did the exploration — the goal is you
spend your tool calls on the *new* task, not re-discovering any of this.

**Lineage:** the `counterreword` branch (original Jarvis + the user's own
`counterreword.patch` + Phases 1–9 of a token-optimization plan) has been
merged into mainline — it's not a separate branch state to track anymore,
just history. On top of that, four bugfix patches and eight enhancement
patches (see "Outstanding ideas" and "Patches delivered so far" below) have
since been delivered, and all 8 originally-outstanding enhancement ideas are
now built — there is nothing left outstanding in
`jarvis-token-optimization-enhancements.md`. If you're given a fresh zip,
diff it against what's described here before assuming anything is still
broken or still just a design — most of what used to be wrong has already
been fixed. Several further patches have since landed on top of all of that
(none part of the original enhancements doc):

- `jarvis-provider-override.patch` (+ two follow-ups), a `--provider NAME`
  CLI override that grew into an ordered-list, ranked-picker override — see
  "Provider/key failover" below.
- `jarvis-provider-order-picker.patch`, the web UI's multi-pick ordered
  provider picker — same section.
- `jarvis-persist-console-dump.patch`, saving a turn's console-dump extra
  server-side so it survives a page reload — see "Console-dump persistence"
  below.

See "Patches delivered so far" for the full list and apply order.

Package root: `jarvis-cli/jarvis/*.py`. All paths below
are relative to that `jarvis/` directory.

---

## What this system does

Jarvis is a CLI assistant that calls out to an LLM (Gemini/Groq/OpenAI-
compatible/others) with a large catalog of local tools (desktop automation,
saved commands, Spotify, git, files, package management, etc.). The
token-optimization work exists because sending the *entire* tool catalog's
schemas on every single message was expensive (~7.9k prompt tokens). The fix
is a local (no-model, no-token-cost) router that guesses which tool **group**
is relevant from keywords, and only sends that group's schemas — falling back
to a tiny "discovery" tool when it has no opinion, rather than the full
catalog.

**Important:** `jarvis` is a brand-new OS process on every `jarvis ...`
invocation (see `history.py`'s module docstring). Nothing is held in memory
between separate CLI invocations — conversation history persists on disk via
`conversations.py`, not via any in-process state.

---

## Request flow

```
user message
   │
   ▼
tool_router.route(user_text)         # tool_router.py — pure keyword scoring, zero tokens
   │
   ├─ confident (real keyword hit)   → active_schemas = schemas_for_tools(route.tools)
   │                                    (that group's tools only)
   │
   └─ not confident                  → active_schemas = DISCOVERY_AND_COMMANDS_SCHEMAS
                                         (just search_tools + search_commands)
   │
   ▼
ai_client.ask()  →  for each eligible provider, for each API key:
   │        _build_messages() assembles system prompt + history + commands
   │        listing (skipped if router is confident about some OTHER group)
   │        + memory context (already lazy/relevance-scored internally)
   ▼
adapter (ai_providers.py: call_gemini / call_openai_compatible / etc.)
   │  rebuilds tools_payload FRESH every round from the current active set
   │  (never resends a stale/growing list) — round loop is MAX_TOOL_ROUNDS=5
   ▼
tool call → _make_tool_executor()'s _executor(name, arguments)
   │  runs the tool, shapes the result (tool_result_shaping.py), caches it
   │  (so a failover to the next key never re-runs a real action)
   │  IF the tool was search_tools/search_commands and found something,
   │  discover_sink() grows active_schemas IN PLACE so the match is
   │  actually callable on the very next round (same ask(), no resend of a
   │  whole extra "discovery" round trip)
   ▼
next round sees the grown active set → eventually a final text answer
```

---

## File map

| File | Role | Key symbols you'll want |
|---|---|---|
| `tool_router.py` | Local keyword router, zero model cost | `MIN_SCORE=5`, `ROUTER_MAX_GROUPS=2`, `ROUTER_GROUP_MARGIN=10` (weighted multi-group trimming — enhancement #1), `RouteResult` (`.tools`/`.groups`/`.confident`/`.matches` — `.matches` is a list of `(group, tool_name, matched_phrase)` triples recorded as each qualifying keyword hit activates a group, enhancement #10; inert debug data, no caller branches on it), `route(user_text)` |
| `tool_registry.py` | Static data: which tools exist in which group, what keywords activate them, per-group workflow guidance | `TOOL_GROUPS`, `TOOL_KEYWORDS` (values may be a plain int weight or `{"weight", "not_with"}` — enhancement #3), `TOOL_PACK_INSTRUCTIONS`, `TOOL_INDEX`, `group_of()`, `tools_in_group()`, `keywords_for()`, `keyword_weight()`, `keyword_exclusions()`, `pack_instruction()` |
| `discovery_cache.py` | Small on-disk, cross-process cache of recent `search_tools`/`search_commands` hits (enhancement #2) — new file, no prior counterpart | `CACHE_FILE=~/.jarvis/discovery_cache.json`, `TTL_SECONDS=300`, `MAX_ENTRIES=20`, `cache_lookup(query, kind)`, `cache_store(query, kind, found)` |
| `tools.py` | Full tool schema catalog + schema-shrinking helpers + the `search_tools` discovery tool | `CORE_TOOL_SCHEMAS`, `TOOL_SCHEMAS` (the full catalog), `DISCOVERY_TOOL_SCHEMAS` (just `search_tools`), `DISCOVERY_AND_COMMANDS_SCHEMAS` (`search_tools` + `search_commands`, added later — see bug log), `schemas_for_tools()`, `compact_schemas_for_prompt()`, `name_only_schemas_for_prompt()`, `tool_search_tools()`, `_SEARCH_TOOLS_CAP=8` |
| `command_tools.py` | Saved-command system: search/run/create/update user-defined command routines | `COMMAND_TOOL_SCHEMAS`, `COMMAND_TOOLS`, `tool_search_commands()`, `_resolve_command()` (returns `needs_clarification: True` on a miss), `resolved_run_for_review()`, `command_call_requires_confirmation/ai_review()`, `_LIST_ALL_CAP=40` |
| `ai_client.py` | The orchestrator: `ask()`, message/prompt assembly, the shared tool executor, provider/key failover loop | `ask()` (now also cache-pre-seeds `active_schemas` from `discovery_cache` on a not-confident route — enhancement #2; also fires an optional `on_route(route)` callback right after `tool_router.route()` runs — enhancement #10, see `cli.py` row below), `_build_messages()`, `_make_tool_executor()` (+ its `discover_sink` hook and `cache_query` param, which feeds `discovery_cache.cache_store()` on a `search_tools`/`search_commands` hit), `_FAILED_LOOKUP_THRESHOLD=3`, `_REPEAT_FAILURE_DETECTORS` (generalized per-kind repeat-failure registry — enhancement #5; the old single-purpose `_failed_lookups`/`_commands_surfaced` pair is gone, replaced by a `_repeat_failures` count dict + `_surfaced` set inside `_make_tool_executor()`), `OrderedSchemaSet` (insertion-ordered, name-keyed structure — enhancement #6; `ask()`'s `active_schemas`/`compact_schemas`/`name_only_schemas` are instances of it now, not plain lists, so a duplicate tool name is structurally impossible to add twice — `.to_list()` converts at the boundary right before a provider adapter sees the final `tool_schemas`), `_tool_runs_note(runs, char_budget, verbosity="full")` (now re-applies `tool_result_shaping.shape_result()` to each cached run's result at the *current* round's `verbosity_ref[0]` before serializing it into the note, instead of resending whatever verbosity it was originally shaped at — enhancement #8; `_extras_from_runs()`/the persisted history record is untouched, only what's resent to the model each round is retrimmed), `_spawn_title_update()`/`run_internal_retitle()` (titling now launches a **detached OS subprocess**, not a daemon thread — see bug log #8; the subprocess re-reads the exchange from disk rather than needing it passed on the command line), `PROMPT_MODE_DEFS` (capacity modes), `_prompt_profile()`, `_eligible_providers()`, `AskResult` (`.usage` — see "Token usage instrumentation" below) |
| `ai_providers.py` | Per-provider API adapters + the tool-round loop | `MAX_TOOL_ROUNDS=5` (**never lower this to save tokens** — optimize what's sent per round instead), `ADAPTERS`, `call_gemini()`, `call_openai_compatible()`, each with its own `_tools_payload()` rebuilt fresh every round; `set_log_context()`/`_record_usage()`/`_call_tool_safely()`/`get_usage_summary()` (real per-round provider-reported tokens + per-tool-call ~estimates — see "Token usage instrumentation" below) |
| `token_usage.py` | Token measurement helpers, shared by both repos | `estimate_tokens_for(obj)` (~4 chars/token heuristic — tool call args/results, or anything else no provider reports a count for on its own), `extract_usage(provider_type, data)` (pulls real provider-reported prompt/completion tokens out of one raw response body; returns `None` if that response carried no usage block) |
| `tool_result_shaping.py` | Trims what a tool call **returns**, after execution (the output-side counterpart to `tools.py`'s schema shrinking) | `TOOL_RESULT_SPECS` (opt-in allowlist — unlisted tools pass through unchanged), `shape_result(name, result, verbosity)` |
| `tool_safety.py` | Confirmation/AI-review gating, independent of token optimization — **do not touch for this work** | `requires_confirmation()`, `requires_ai_review()`, `DEFAULT_AI_REVIEW` |
| `conversations.py` | On-disk history, since each CLI call is a fresh process | `conversation_messages()` (recent turns + recap of older ones; each older-exchange recap line now also gets a compact `" [recap] ran tool(args), tool(args)"` suffix built from that exchange's stored `extras` — enhancement #9 — via new helpers `_extras_recap_fragment()`/`_compact_args()`; name-and-key-argument only, no result payloads), `append_exchange()`, `other_conversations_context()` |
| `memory.py` | Cross-session memory | `prompt_context(compact, query, extra_texts)` — already relevance-scores against `query` and returns `""` on no match |
| `desktop_tools.py` | Mouse/keyboard/window schemas + implementations | `click`, `focus_window`, `list_windows`, `get_active_window`, `get_window_size`, `get_window_info` (**the last three overlap heavily** — see bug log) |
| `ocr_tools.py` | `click_on_text` — screenshot + local OCR + click, in one call, by visible label text |
| `everything_tools.py` | File search + Windows shell integration (`search_files`, `reveal_in_explorer`, `open_file_location`, `open_file`) | `reveal_in_explorer()` (Windows-only; builds its `explorer /select,"<path>"` call as **one pre-quoted string**, not a list — see bug log #9 for why the list form silently breaks on any path with a space), `open_file_location()`/`open_file()` (`os.startfile()` — no equivalent quoting footgun) |
| `cli.py` | Terminal trace output (the `↳ asking gemini (key n/N)…` / `$ <tool>` / `tokens: in=X out=Y` lines you see in logs) | the `on_attempt` print line, a tool-name→trace-verb dict (e.g. `"search_commands": "searching commands"`), `on_route` (new — enhancement #10; prints one `$ routed: <group> (matched "<phrase>" on <tool>)` line per matched group, first match per group, using `route.matches`; no-ops silently on a non-confident route; always-on stderr trace like the others, not gated behind any `--verbose`/env-var flag — no such flag exists anywhere in `cli.py` today), a hidden `_internal_retitle <conv_id> <exchange_count>` dispatch branch (see bug log #8 — not in `--help`, only `ai_client._spawn_title_update()` should ever invoke it) |
| `tests/test_schemas_for_tools.py`, `tests/test_enhancements.py` | Automated tests — see "Testing pattern used in this project" below | run directly with `python3 tests/test_<name>.py`; no framework dependency |
| `tests/interactive_inspector.py` | Human-readable diagnostic report (not pass/fail) for what a given message actually triggers — see "Interactive inspector" below | REPL / single-shot / `--examples` batch modes; `report(text)`, `_explain_keyword_matches()`, `_trim_groups()`, `_active_schemas_for()` |
| `tests/benchmark_pc_actions.py`, `tests/compare_benchmark_results.py` | Cross-repo (main vs. counterreword) token/tool-call benchmark — see "Cross-repo benchmark" below | `run_benchmark(label)`, `is_safe_tool()` (default-deny allowlist gating which tools are allowed to actually run), `install_stub()`; `compare_benchmark_results.py` reads two of the JSON files this produces and prints a delta |

Groups **confirmed** (by direct testing, not just reading) so far:
- `desktop` = `type_text, press_key, hotkey, scroll, move_mouse, click, drag, get_screen_size, get_mouse_position, list_windows, focus_window, get_active_window, get_window_size, get_window_info, take_screenshot, click_on_text` (16 tools)
- `commands` = `search_commands, run_command, run_chain, create_command, update_command, run_custom_command` (6 tools)

Other groups exist (`web`, `spotify`, `youtube`, `system_control` — which
covers **both** git and package tools, not two separate groups as an early
planning doc guessed — `files`, `playnite`, plus likely `memory`/`system`/
`capacity`) but weren't individually enumerated here. Check
`tool_registry.TOOL_GROUPS` directly before assuming a specific tool's group
membership.

---

## Capacity modes

Four modes (`full`/`compact`/`precise`/`ultra`) control, per provider, both
**how verbose the tool schemas sent are** (`tool_schema_style`: compact vs.
raw/full vs. name-only) and **how verbose tool results are** once they come
back (`tool_result_verbosity`: full/medium/low, consumed by
`tool_result_shaping.py`). These are resolved per-provider fresh on every
attempt (`_prompt_profile()`), because a legacy `compact_prompt_providers`
config lets mode vary by provider — e.g. a smaller-context provider could
reasonably be pinned to a leaner mode than the default. This is orthogonal to
routing/discovery — modes control *verbosity*, routing controls *which tools
exist in the payload at all*.

---

## Provider/key failover

`ai_client.ask()` tries each eligible provider (`_eligible_providers()`—
skips disabled or key-less entries), and within a provider, every configured
API key in order, labeled `"{provider} (key i/N)"` in traces and logs. **One
`_make_tool_executor()` instance is shared across the entire `ask()` call**
(every provider, every key) — this is deliberate: it's what makes tool
results cache-and-reuse across a failover (a retried key never re-runs a
real action) and what lets `discover_sink`'s discovered-names tracking
persist across the whole `ask()`, not reset per attempt.

A key only advances to the next one **on failure** (`result.ok is False`).
This matters: if a payload-level bug (see Bug 4 below) makes a request fail
identically regardless of which key is used, every single key gets tried
and fails in sequence — that's pure wasted latency, not tokens, and it looks
exactly like "why is this so slow" with no token cost to show for it.

**Provider override (one-off, per-call):** `ask()` takes an optional
`provider_override`, matched case-insensitively against each provider's
`"name"` field (same lookup `provider_priority` uses). It accepts either
shape:

- a single provider name (string) — the original form, still what the
  plain-CLI `--provider NAME` flag sends; or
- an ordered list/tuple of names, e.g. `["anthropic", "gemini"]` — try
  anthropic first, then gemini, in exactly that order, via
  `_order_providers_by_override()`. This is the shape the web UI's
  multi-pick provider-order picker sends (see below) and what
  `--provider NAME1,NAME2,...` sends from the plain CLI
  (`cli._parse_provider_override_value()` splits on commas).

Either way, `_eligible_providers()`'s normal output is filtered/reordered
down to just the requested name(s) before the attempt loop above ever runs,
so `provider_priority` and every other configured provider are skipped
entirely for that single call — multi-key failover *within* each named
provider still applies normally. It mirrors the existing one-off "mode"
argument to `jarvis tool-run`/`tool-preview`: nothing persisted to
`ai_config.json`, no effect on any other call. If a name doesn't match an
eligible provider (wrong name, disabled, or no usable key), `ask()` returns
the same failure shape as "no providers configured" except
`result.attempts` names the requested provider(s), so the reason shows up
in the normal trace/error message instead of silently falling back to the
priority order. Wired into the CLI as `jarvis --provider NAME <message>` /
`jarvis <message> --provider=NAME` / `jarvis --provider NAME1,NAME2
<message>` — `cli._extract_provider_override()` strips the flag out of
`argv` before the rest is joined into the free-text message, so `--provider`
can appear anywhere in the typed command. Covered by
`tests/test_provider_override.py` (10 tests: argv extraction incl.
`--provider=`, mid-sentence placement, absent/trailing/duplicate-flag edge
cases; and override name-matching incl. case-insensitivity, a disabled
provider, an unknown name, and priority being bypassed) — those tests
predate the list form and only exercise the single-name path; the list
form's ordering (`_order_providers_by_override`) has no dedicated test file
as of this writing.

**Naming note:** `ask()`'s own docstring calls this "the Ask panel's
multi-pick provider-order picker (see ... `jarvis-provider-override-ranked
.patch`)" — that exact filename was never actually delivered. The backend
support (`_order_providers_by_override`, the list-form `provider_override`)
landed on its own, ahead of any frontend for it, under the assumption a
matching web UI patch would follow with that name. The web UI patch that
actually implements the picker is named `jarvis-provider-order-picker.patch`
(see "Patches delivered so far") — same feature, different filename. Don't
go looking for `jarvis-provider-override-ranked.patch`; it doesn't exist.

The web UI exposes this as a picker: a "Provider: Auto" button next to
Clear in the Ask panel header lists every currently-eligible provider (via
`GET /api/ai/providers` in `server.js`, which hand-mirrors
`_eligible_providers`/`_sort_providers_by_priority` in JS rather than
calling them, so keep both in sync by hand if either changes). It was
originally single-pick (`jarvis-provider-override-web-ui.patch`: click one
provider, or "Auto"); `jarvis-provider-order-picker.patch` replaced that
with the current multi-pick behavior — clicking providers in the menu
appends each to an ordered pick-list (`state.providerOverride`, an array
now, not a single name-or-null), with a numbered badge on each selected
item showing its position, and clicking a picked one again removes it
(everything after renumbers). The menu stays open across picks so multiple
providers can be chosen in one visit, closing only on "Auto", Escape, or an
outside click. The picks are joined with `,` (`providerOverrideValue()`)
and sent as `msg.provider` on every "ask"/redo WS message — `server.js`'s
`"ask"` handler validates that string against
`/^[A-Za-z0-9_-]{1,64}(,[A-Za-z0-9_-]{1,64}){0,9}$/` (widened from a
single-name-only regex to allow up to 10 comma-separated names) before
setting it as `JARVIS_PROVIDER_OVERRIDE` in the spawned subprocess's env —
consumed by `cli.py`'s `handle_ai_prompt` (see
`jarvis-provider-override-cli-env.patch`) only when no argv `--provider`
flag was already given, then split back into a list by
`cli._parse_provider_override_value()` exactly like a typed
`--provider a,b,c` would be. Client-side-only choice: nothing persisted
server-side, resets to Auto on a full page reload.

---

## Console-dump persistence

Some provider replies come back with extra raw text glued around the real
answer — either literal command stdout a weaker/local model echoes ahead of
its answer, or inline tool-call/tool-result scaffolding it echoes instead of
using real function-calling (`ai_client._TOOL_TRACE_LINE`). The web UI has
always split this out live into its own "Console" bubble while streaming
stdout for a fresh ask (`app.js`'s `splitConsoleDump()`), but until
`jarvis-persist-console-dump.patch`, only the *unsplit* raw text was ever
saved to the conversation record (`conversations.append_exchange`'s
`jarvis_text` arg) — so a page reload replayed the raw glued-together text
in the main reply bubble instead of a clean reply + separate Console bubble
the way it looked live. The other extra types (screenshot/download/
organizeJson/confirm) didn't have this gap because `ai_client
._extras_from_runs()` already turned runtime results into saved extras
before this; "console" just wasn't one of them yet.

The fix is `ai_client._split_console_dump()`, a line-for-line Python port of
`app.js`'s `splitConsoleDump()`/`NAME_PREFIX_LINE` (reusing the existing
`_TOOL_TRACE_LINE` regex instead of re-declaring the JS's
`INLINE_TOOL_TRACE_LINE`). `ask()`'s `append_exchange` call site now runs
`result.text` through it and saves the *clean* text as the exchange's
`jarvis` field, plus — when there was anything to split out — a
`{"type": "console", "data": {"dumpLines": [...]}}` extra, the same shape
`_extras_from_runs()` already produces for the other extra types. The web
UI already had a `"console"` case in `renderThreadExtra()` (it just never
had anything to replay) and generic bucket/replay plumbing
(`seedThreadExtrasFromRecord`/`loadConversationIntoThread`) that needed no
changes at all — this was purely a "the server wasn't saving it" gap, not a
missing client feature.

**Only the saved copy is split — live behavior is unchanged.** `ask()`
still returns/prints the raw, unsplit `result.text` (`AskResult.text`), since
a plain terminal and the browser's live stdout stream both still need the
whole thing exactly as before; the browser keeps doing its own live split
via `splitConsoleDump()` as the stream arrives, same as always. The
server-side split only affects what a *reload* sees afterward, bringing it
in line with what was already shown live.

No recap fragment was added for the new `"console"` extra type in
`conversations._extras_recap_fragment()` — unlike a real tool call, a
console dump isn't something the model needs reminded of on a later turn,
so it's deliberately left out of the `[recap] ran ...` summary.

Covered by 4 new tests appended to `tests/test_enhancements.py` (now 30
tests total): no-op on plain text, splitting out leading raw output ahead
of a `"<Name>: <rest>"` line, splitting out an inline tool-trace line
elsewhere in the reply, and the empty/`None` edge cases.

---

## Bug-fix history (don't re-break these / don't re-discover these)

1. **False keyword match via raw substring.** `tool_router.route()` used
   `if phrase in text`, so `"commanded"` matched the keyword `"command"` and
   misrouted to the `commands` group for messages that had nothing to do
   with saved commands. **Fixed:** word-boundary regex (`\bphrase\b`).

2. **Missing desktop keywords.** `TOOL_KEYWORDS` had no entries for
   `"discord"`, `"chrome"`, `"firefox"`, bare `"window"`, or bare `"click"` —
   so common desktop-automation phrasing didn't confidently route to
   `desktop` at all. **Fixed:** added to the `click` and `focus_window`
   entries.

3. **Expensive empty-query dump.** `tool_search_commands()`'s empty-query
   path built full summaries (name + description + vars) for up to 40
   commands — ~600 output tokens for "what commands exist", every time.
   **Fixed:** empty query now returns names only, matching how
   `tool_search_tools()`'s own empty-query path already behaved.

4. **Tool/command ambiguity → endless `search_tools` retries.** When the
   router wasn't confident, only `search_tools` was offered — never
   `search_commands` — so "run the tts thing" (a saved command, not a
   built-in tool) caused the model to retry `search_tools` with different
   keywords forever, finding nothing. **Fixed two ways:** (a) new
   `DISCOVERY_AND_COMMANDS_SCHEMAS` constant offers both discovery tools
   together whenever the router has no opinion; (b) a
   `_FAILED_LOOKUP_THRESHOLD=3` counter inside `_make_tool_executor()`
   force-activates the whole `commands` group after 3 failed lookups
   (`search_tools` empty match, or `run_command`/`run_chain` returning
   `needs_clarification`), which also covers the case where the router was
   *confidently* pointed at the wrong group the whole time — fix (a) alone
   doesn't help there. (b)'s counter is now one entry in the generalized
   `_REPEAT_FAILURE_DETECTORS` registry — see enhancement #5 below —
   with identical threshold/corrective, so this fix's behavior is
   unchanged.

5. **No desktop workflow guidance → 6 rounds instead of ~3.**
   `get_active_window`/`get_window_info`/`get_window_size` heavily overlap,
   and `click_on_text` (screenshot+OCR+click by label, one call) existed but
   nothing steered the model toward it over "get window rect, guess x/y,
   click." **Fixed:** added explicit guidance to `TOOL_PACK_INSTRUCTIONS
   ["desktop"]` — don't stack the three overlapping inspectors; use
   `click_on_text` instead of size-then-guess-coordinates. (This is
   steering, not a hard guarantee — the model can still take the verbose
   path occasionally.)

6. **Regression introduced by fix #4, fixed same session.** Making
   `search_commands` directly callable from turn one meant a *direct*
   `search_commands` call never triggered `discover_sink` the way
   `search_tools` matches already did — so finding the right saved command
   still cost an extra round before `run_command` became callable.
   **Fixed:** any `search_commands` call returning ≥1 match now also calls
   `discover_sink(["run_command"])` to activate the rest of the `commands`
   group immediately (search_commands itself is already known, so only its
   siblings get added).

7. **No dedupe guarantee → total provider failure.** Nothing guaranteed a
   tool name couldn't appear twice in what's actually sent to a provider.
   Gemini's API **hard-rejects the entire request** (`HTTP 400: Duplicate
   function declaration found: X`) if that happens — identically on every
   key tried afterward. This is the likely root cause of an observed "10
   Gemini keys + 4 Groq keys, all instantly failed" log — pure wasted
   latency, no tokens spent, which is why it can look like "it's just slow"
   rather than "it's burning tokens." **Fixed:** defensive dedupe-by-name
   (first occurrence wins) added at the one point every provider passes
   through (`ai_client.ask()`, right before `tool_schemas` is handed to the
   adapter). **Superseded by enhancement #6:** that filter guarded against
   a duplicate reaching the adapter but didn't prevent one existing
   upstream in `active_schemas`/`compact_schemas`/`name_only_schemas`
   themselves; those are `OrderedSchemaSet` instances now, so a duplicate
   name can't be added to any of them in the first place, and the filter
   itself was removed as redundant.

8. **New conversations never actually got a title/description.**
   `_spawn_title_update()` ran `_maybe_update_title()` on a **daemon
   thread** so titling (a real ~1-30s network round trip) would never
   delay the visible reply. But `jarvis` is a brand-new OS process on
   every invocation (see `history.py`'s docstring) whose top-level call is
   `sys.exit(handle_ai_prompt(...))` — and Python kills daemon threads
   outright on interpreter shutdown rather than waiting for them. Since
   the process exits within milliseconds of printing the reply, the
   background thread lost that race on **effectively every call**, not
   just occasionally — reproduced directly: a daemon thread with
   `time.sleep(1)` never gets to run before `sys.exit(0)` returns. **Fixed:**
   `_spawn_title_update(conversation_id, exchange_count)` now launches a
   fully **detached OS subprocess** (`jarvis _internal_retitle <id> <n>`,
   own session/process group, detached stdio) instead of a thread — it
   survives the parent's exit. The new hidden entry point
   `ai_client.run_internal_retitle()` re-reads the just-appended exchange
   straight from the conversation's on-disk record (already written by
   `append_exchange()` before the subprocess is even launched) rather than
   needing the text passed on the command line. Dispatched via a new
   `_internal_retitle` branch in `cli.py` — undocumented in `--help`,
   nothing but jarvis itself should call it. Verified end-to-end (spawn,
   let the "parent" exit immediately, confirm the title landed on disk
   afterward in a separate check) rather than just read as plausible.

9. **`reveal_in_explorer` silently opened the wrong (default) folder
   instead of revealing the target file — for any path containing a
   space.** The code built the Explorer call as
   `subprocess.Popen(["explorer", f"/select,{path}"])`. Passed as a
   **list**, Python's own Windows argv-quoting (`list2cmdline`) wraps the
   *entire* `/select,<path>` token in quotes whenever the path has a
   space, producing `explorer "/select,C:\Users\John Doe\My File.txt"` —
   confirmed directly via `subprocess.list2cmdline()`. But `explorer.exe`
   needs the quotes to start **right after the comma**:
   `explorer /select,"C:\Users\John Doe\My File.txt"`. With the quotes in
   the wrong place, Explorer can't parse the argument at all and silently
   falls back to opening its default folder (commonly Documents) instead
   of erroring — no exception, no error dict, so this looked like "the
   button doesn't work" with no clue why. Hits any path with a space in
   it — filenames, "My Documents", etc. — which is most real-world paths.
   **Fixed:** build the whole command as one pre-quoted string —
   `subprocess.Popen(f'explorer /select,"{path}"')` — which sidesteps
   Python's own Windows quoting entirely and puts the quotes exactly
   where Explorer expects them. `open_file_location`/`open_file` were
   never affected — they use `os.startfile()`, which has no equivalent
   quoting step.

---

## Known-good invariants — don't relitigate these

- `TOOLS`/`CORE_TOOL_SCHEMAS` stay fully loaded and locally executable at
  all times; only what's **sent to the model** is filtered.
- Never touch `tool_safety.py`, confirmation prompts, `risk_review()`, or
  the AI-review gating in `_make_tool_executor()` as part of token-opt work.
- `MAX_TOOL_ROUNDS` stays at 5 — optimize per-round payload, not round count.
- `tools_payload` (whatever it's called per-provider) is already rebuilt
  fresh every round in every adapter — don't reintroduce an ever-growing
  resent list.
- Saved-commands listing in the system prompt is already lazy (skipped when
  the router is confidently on some other group) — same for schema/result
  verbosity being resolved per-provider, per-attempt.

---

## Outstanding ideas (designed, not implemented)

A separate doc, `jarvis-token-optimization-enhancements.md`, has full
implementation designs (no code, when first written) for 8 ideas. **All 8
are now built** (see "Patches delivered so far" below) — the doc itself is
unchanged (still the original design), so treat every numbered item there as
historical design rationale, not a to-do. Nothing is currently outstanding
from this doc:

- ~~#1 weighted multi-group router confidence~~ — done, `tool_router.py`.
- ~~#2 cross-process discovery cache~~ — done, new `discovery_cache.py` +
  `ai_client.py`.
- ~~#3 exclusion keywords in `TOOL_KEYWORDS`~~ — done, `tool_registry.py` +
  `tool_router.py`.
- ~~#5 generalized repeat-failure counter~~ — done, `ai_client.py`
  (`_make_tool_executor()`'s `_REPEAT_FAILURE_DETECTORS` registry; see Bug
  4's note above for how the pre-existing tool/command case maps onto it).
- ~~#6 dict-keyed active-schema construction~~ — done, `ai_client.py` (new
  `OrderedSchemaSet` class; see Bug 7's note above for how it supersedes
  that fix's defensive dedupe filter).
- ~~#8 shape historical tool-run notes at current verbosity~~ — done,
  `ai_client.py` (`_tool_runs_note()` takes a `verbosity` param now and
  re-applies `tool_result_shaping.shape_result()` to each cached run before
  serializing it; its one call site passes `verbosity_ref[0]`). Only what's
  resent to the model each round is affected — `_extras_from_runs()`/the
  persisted history record still keeps the originally-cached result.
- ~~#9 recap-level summarization of prior tool calls~~ — done,
  `conversations.py` (`conversation_messages()`'s older-exchange recap
  lines now append a short `_extras_recap_fragment()` built from that
  exchange's stored `extras`, instead of relying only on the assistant's
  free-text description of what it did). Note: since `extras` (see
  `ai_client._extras_from_runs()`) only captures specific side-effects
  (confirm/screenshot/organizeJson/ytdl_download) rather than a fully
  generic tool-call log, the recap fragment summarizes those, not literally
  every tool call from the turn — a smaller scope than the design doc's
  `"ran focus_window(Discord), click_on_text('Call')"` example implies.
  Widening that would need `_extras_from_runs()` itself to also record a
  plain `{"type": "call", ...}` entry for runs that don't already produce
  one.
- ~~#10 log the router's activation reason~~ — done, `tool_router.py`
  (`RouteResult.matches`) + `ai_client.py` (`ask()`'s new `on_route`
  callback, fired right after routing) + `cli.py` (the `on_route` trace
  printer — see its file-map row above). Not gated behind a debug/verbose
  flag as the design doc suggested, since no such flag exists in `cli.py`;
  it's an always-on stderr trace like `on_attempt`/`on_tool_call` are.

---

## Testing pattern used in this project

No test suite framework dependency is required (plain asserts throughout),
though the tests are also valid pytest functions if pytest happens to be
available in your environment. Two files live in `tests/` (repo root, next
to `jarvis-cli/`, not inside it):

- `tests/test_schemas_for_tools.py` — the original baseline checks:
  `schemas_for_tools()` purity, `TOOL_GROUPS`/`TOOL_INDEX` consistency
  (every tool grouped, no tool in two groups, no ghost names), and basic
  router sanity (empty input, ambiguous greeting, a handful of known-good
  group matches).
- `tests/test_enhancements.py` — covers enhancements #1–#3, #5, and #6 (26
  tests total as of the last patch that touched this file):
  router score trimming (near-tie keeps both groups, a weak group beyond
  `ROUTER_GROUP_MARGIN` gets dropped, `ROUTER_MAX_GROUPS` caps even a
  within-margin third group), `discovery_cache.py` (round-trip, kind
  isolation, TTL expiry, the `MAX_ENTRIES` cap, corrupt-file handling —
  all isolated to a temp file, never touching a real `~/.jarvis`) and its
  wiring into `_make_tool_executor()`'s `cache_query` param, the
  `TOOL_KEYWORDS` exclusion-keyword shape (`keyword_weight()`/
  `keyword_exclusions()`, and that an excluded phrase actually cancels a
  match while a plain phrase is unaffected), the generalized
  `_REPEAT_FAILURE_DETECTORS` registry (the preserved tool/command
  behavior, the new per-text `click_on_text` detector, that unrelated
  kinds count independently, and that a direct `search_commands` hit
  marks `"tool_or_command"` surfaced so it can't double-fire later), and
  `OrderedSchemaSet` (dedup-first-occurrence-wins, insertion order
  preserved through `.extend()`, membership/`bool()`, tolerance of
  non-dict/nameless entries, plus a test that reconstructs `ask()`'s real
  `active_schemas`/`compact_schemas`/`name_only_schemas`/`_discover_sink`
  wiring — the same "mirror the internal construction" approach
  `tests/interactive_inspector.py` already uses — to confirm
  re-discovering an already-present name is a true no-op and all three
  sets stay duplicate-free and in lockstep). The repeat-failure tests
  route tool calls through fake `execute_tool` stand-ins with varied
  arguments per call — real duplicate `(name, args)` calls hit
  `_make_tool_executor()`'s own result cache and return early, so
  identical arguments would silently under-count a genuine repeat-failure
  streak in a test.

  Enhancements #8, #9, and #10 do **not** yet have dedicated automated
  tests here — they were verified with inline snippets only (see step 2
  below) at the time they were built. Add coverage for them the same way
  #1–#3/#5/#6 were covered if you're touching any of the three again.

Run either file directly:

    python3 tests/test_schemas_for_tools.py
    python3 tests/test_enhancements.py

**Path note:** both files' `sys.path` setup points at `jarvis-cli/` (where
the `jarvis` package actually lives — see "Package root" at the top of
this doc), not at the repo root the `tests/` folder sits next to. An
earlier version of `test_schemas_for_tools.py` pointed one level too high
and raised `ModuleNotFoundError: No module named 'jarvis'` when run
exactly as its own docstring said to; this was fixed alongside adding
`test_enhancements.py`, and any new test file added later should use the
same `Path(__file__).resolve().parent.parent / "jarvis-cli"` pattern.

What's worked well beyond running these files, in order:
1. `python3 -m compileall jarvis/<changed_files>.py -q` — catches syntax
   errors immediately.
2. A short inline Python snippet (`sys.path.insert(0, ".")` from
   `jarvis-cli/`, then import the specific module and call the changed
   function directly — e.g. `tool_router.route("...")`, or hand-simulating
   `discover_sink`'s dedupe logic) against the **exact repro text** from
   whatever log/complaint prompted the fix. Cheap, fast, and directly
   verifies the specific bug is gone without needing real API keys or a
   live model.
3. A quick regression check with 1-2 adjacent phrasings (e.g. after the
   word-boundary fix, also checking `"recommend"` — which also contains
   `"command"` as a substring — doesn't false-positive, and that legitimate
   `"run my saved command"` phrasing still matches).
4. Once a change has a real regression risk (not just a one-off repro), add
   it to `tests/test_enhancements.py` (or a new `tests/test_<topic>.py`
   following the same no-dependency plain-assert pattern) instead of
   leaving it as a throwaway snippet — that's what keeps step 2's one-off
   checks from silently rotting the next time something nearby changes.
5. For a *human* to eyeball what a message actually does end to end —
   which keywords matched, what the router decided, whether a discovery-
   cache pre-seed kicked in, which schemas would actually be offered, and
   a rough token count — use `tests/interactive_inspector.py` (see next
   section) instead of an inline snippet; it's built exactly for that and
   already handles the active_schemas/cache-pre-seed mirroring so you
   don't have to hand-reconstruct it in a REPL each time.

---

## Interactive inspector (`tests/interactive_inspector.py`)

A human-readable "what would actually happen for this message" report —
not a pass/fail test. For any message it prints: every `TOOL_KEYWORDS`
phrase that word-boundary-matched (including ones cancelled by an
exclusion — enhancement #3), each group's accumulated score before/after
the `ROUTER_MAX_GROUPS`/`ROUTER_GROUP_MARGIN` trim (enhancement #1), the
real `tool_router.route()` result, whether a `discovery_cache` hit would
pre-seed `active_schemas` and from what prior query (enhancement #2), the
matched group's `TOOL_PACK_INSTRUCTIONS` text, the actual schemas that
would be offered that round, and a rough chars/4 token estimate for that
set vs. the full catalog (an order-of-magnitude signal, not a real
tokenizer count).

Three modes:

    python3 tests/interactive_inspector.py
        REPL — type a message, see its report, repeat ('quit'/'exit'/
        Ctrl-D to stop). Reads/writes your REAL ~/.jarvis/discovery_cache.json
        (via discovery_cache.py itself, unmodified), so what you see is
        exactly what a real `jarvis ...` invocation would do right now —
        including genuine cache hits left over from actual prior use.

    python3 tests/interactive_inspector.py "download this youtube video"
        Single-shot — one report, then exit. Same real-cache behavior as
        the REPL; useful for scripting/piping or a quick one-off check.

    python3 tests/interactive_inspector.py --examples
        Batch/automatic mode — runs six curated messages chosen to
        exercise every enhancement (baseline single-group, a near-tie that
        keeps both groups, a weak group dropped beyond the margin, a third
        group capped by `ROUTER_MAX_GROUPS`, an exclusion-keyword cancel,
        and a `discovery_cache` HIT demo) and prints a report for each.
        Isolated to a throwaway cache file the same way
        `tests/test_enhancements.py`'s `_isolated_cache()` is, so it never
        touches your real `~/.jarvis`. Good for a quick eyeball pass after
        touching `tool_router.py`/`tool_registry.py`.

**Important caveat — this file mirrors, not reuses, two bits of internal
logic** that aren't exposed as standalone functions elsewhere:
`tool_router.route()`'s scoring/trim algorithm (originally mirrored so it
could also capture per-phrase match detail for display, since `RouteResult`
didn't carry that — now that enhancement #10 has landed, `RouteResult.matches`
carries exactly that detail natively, so `_explain_keyword_matches()` here
could in principle be simplified to just read `route.matches` instead of
re-deriving it; not yet done, so the mirror and its drift check below still
apply until someone makes that swap) and `ai_client.ask()`'s `active_schemas`
construction including the enhancement #2 cache pre-seed. Every report
cross-checks its own mirrored trim against a real `tool_router.route()` call
and prints a loud `MIRROR DRIFT` warning (not a crash) if they ever disagree.
**If you change `tool_router.route()`'s algorithm or `ai_client.ask()`'s
`active_schemas` construction, update the mirrors in this file in the same
change** — the drift warning is a safety net for catching a missed update,
not a substitute for remembering to make one.

---

## Token usage instrumentation ("Phase 0", new_plan.md)

Both `jarvis-main` and `jarvis-counterreword` carry the **same** usage-
tracking plumbing — it's not counterreword-only, despite one stale,
already-superseded `.rej` hunk in a counterreword zip briefly suggesting
otherwise (that patch's change was already present; the reject was noise
from applying an old patch to a base that had already moved past it — if
you see a `.rej` file, check whether its hunk is already applied before
assuming something's missing).

- `token_usage.py`: `estimate_tokens_for(obj)` (~4 chars/token heuristic)
  and `extract_usage(provider_type, data)` (real provider-reported
  prompt/completion tokens from one raw response body, or `None` if that
  response carried no usage block).
- `ai_providers.py`: `_record_usage()` calls `extract_usage()` on every
  request round and accumulates it; `_call_tool_safely()` estimates
  input/output tokens for every tool call/result via
  `estimate_tokens_for()`; `get_usage_summary()` rolls both up into
  `{input_tokens, output_tokens, total_tokens, rounds: [...], tool_calls:
  [...]}` for the current provider attempt.
- Every adapter's success path returns `AIResult(..., usage=
  get_usage_summary())`; `ai_client.ask()`'s success path threads that
  through as `AskResult(..., usage=result.usage)`.

Net effect: `ai_client.ask(...).usage` already gives you exactly "how many
tokens did this ask cost," broken down by round and by tool call, with
real provider-reported numbers where the provider sent them. Anything that
wants to measure token cost (see "Cross-repo benchmark" below) should read
this directly rather than re-deriving it independently — it's already
correct, and it's identical in both repos.

**Caveat:** if a configured provider's API never returns a usage block
(some Ollama setups, some misconfigured OpenAI-compatible endpoints),
`get_usage_summary()`'s `rounds` list comes back empty for that ask even
though real requests happened — `extract_usage()` only appends a round
when the response actually carried one. Don't read an empty `rounds` list
as "this ask cost 0 tokens"; it means "this provider didn't tell us."

---

## Cross-repo benchmark (`tests/benchmark_pc_actions.py`)

A fixed, repeatable 4-step scenario — quick web search, "go on Discord and
tag @no and say hi," "open the TTS bot," "launch Roblox" — run through the
real `ai_client.ask()` in one continuous conversation, meant to be run
once in `jarvis-main` and once in `jarvis-counterreword` (same
`ai_config.json` provider/model in both — it reads from `Path.home()`, so
this is naturally already shared across both checkouts) to get an
apples-to-apples token/tool-call comparison between the unoptimized
baseline and the optimized branch.

- **Measurement:** reads `AskResult.usage` directly (see "Token usage
  instrumentation" above) rather than re-instrumenting anything itself —
  the numbers it reports are exactly what each repo's own Logs/debug panel
  would already show for the same conversation.
- **Safety:** patches `jarvis.tools.execute_tool` with a **default-deny
  allowlist** (`is_safe_tool()`) — only tools with no real side effect
  (`get_*`/`list_*`/`search_*`, `web_search`, `spotify_now`, etc.) are
  allowed to actually run; everything else (`run_command`,
  `playnite_launch_game`, `click_on_text`, `type_text`, `focus_window`,
  `hotkey`, `reveal_in_explorer`, ...) gets a canned
  `{"ok": true, "simulated": true}` instead of touching real app/OS state.
  Deny-by-default on purpose, so a tool the allowlist's author didn't
  think of gets stubbed rather than silently executed. Verified against
  the real tool catalog (97 tools in counterreword, 96 in main — the only
  difference is counterreword's `search_tools`, itself already read-only)
  by hand before shipping this.
- `compare_benchmark_results.py` takes the two JSON files this produces
  (one per repo) and prints a per-metric and per-step delta, plus one
  bottom-line "X% fewer/more total tokens" verdict.
- **No build/install/PATH step needed to run it.** The script imports the
  `jarvis` package directly from source
  (`sys.path.insert(..., ".../jarvis-cli")`) rather than invoking the
  installed `jarvis` console command, so it always benchmarks whichever
  repo checkout it's run from regardless of what's `pip install`-ed or on
  `PATH` — no need to reinstall/switch anything between a main-branch run
  and a counterreword run. Only the usual runtime dependencies
  (`requests`, etc. — see `jarvis-cli/pyproject.toml`) need to be
  installed once, in whichever Python environment runs the script; they
  can be shared across both checkouts since it's the same dependency set.
- Not yet run against a real provider as of this writing (built and
  syntax/logic-checked, including the safety classification against the
  real tool list, but not a live end-to-end run) — treat its first real
  output with the same "sanity-check before trusting it" caution as any
  new instrument, especially the "no usage block reported" warning path.

---

## Patches delivered so far

Each is a scoped `diff -u` against the `counterreword`-branch zip baseline
(not against each other), so a patch that touches a file already touched by
an earlier patch contains the cumulative diff for that file:

- `jarvis-token-optimization-bugfix.patch` — bugs 1–3 above (`tool_router.py`,
  `tool_registry.py`, `command_tools.py`)
- `jarvis-command-discovery.patch` — bug 4 above (`tools.py`, `ai_client.py`)
- `jarvis-desktop-workflow-guidance.patch` — bug 5 above (`tool_registry.py`,
  cumulative with bug 2's edit to the same file)
- `jarvis-latency-and-dedup-fix.patch` — bugs 6–7 above (`ai_client.py`,
  cumulative with bug 4's edits to the same file)
- `jarvis-token-optimization-enhancements.md` — design doc only, no code,
  covering the (then-)8 outstanding ideas above
- `jarvis-weighted-router-confidence.patch` — enhancement #1 (`tool_router.py`)
- `jarvis-cross-process-discovery-cache.patch` — enhancement #2 (new
  `discovery_cache.py`, plus `ai_client.py`)
- `jarvis-exclusion-keywords.patch` — enhancement #3 (`tool_registry.py`,
  `tool_router.py`; a later re-cut of this patch is diffed against the tree
  with enhancement #1 already applied, not the raw zip baseline, since
  `tool_router.py` had already diverged from baseline by then — check which
  base a `tool_router.py` patch expects before applying if you have more
  than one on hand)
- `jarvis-enhancement-tests.patch` — new `tests/test_enhancements.py`
  (17 tests covering enhancements #1–#3) plus the `sys.path` fix in
  `tests/test_schemas_for_tools.py` (see "Testing pattern used in this
  project" above); scoped against the zip baseline like the others, and
  applies independently of the `tool_router.py`/`tool_registry.py`
  enhancement patches since it only touches `tests/`
- `jarvis-interactive-inspector.patch` — new `tests/interactive_inspector.py`
  (see "Interactive inspector" above); also only touches `tests/`, so it
  applies independently of everything else
- `jarvis-repeat-failure-counter.patch` — enhancement #5 (`ai_client.py`'s
  `_make_tool_executor()`: the old `_failed_lookups`/`_commands_surfaced`
  pair replaced by `_repeat_failures`/`_surfaced` plus the
  `_REPEAT_FAILURE_DETECTORS` registry, with a new `click_on_text`
  same-text-three-times detector alongside the preserved tool/command one)
  and 4 new tests appended to `tests/test_enhancements.py` (now 21 tests
  total in that file — see "Testing pattern used in this project" below);
  diffed against the tree with enhancements #1–#3 already applied (same
  `tool_router.py`/`tool_registry.py` caveat doesn't apply here since this
  patch only touches `ai_client.py` and `tests/`, neither previously
  touched by #1–#3)
- `jarvis-dict-keyed-schema-dedupe.patch` — enhancement #6 (`ai_client.py`:
  new `OrderedSchemaSet` class; `ask()`'s `active_schemas`/
  `compact_schemas`/`name_only_schemas` are instances of it now instead of
  plain lists; `_discover_sink()`'s separate `_discovered_names` set is
  gone in favor of checking membership against `active_schemas` itself;
  the belt-and-suspenders dedupe-by-name filter right before the adapter
  call — added by the Bug 7 fix above — is removed as redundant) and 5 new
  tests appended to `tests/test_enhancements.py` (now 26 tests total);
  diffed against the tree with enhancement #5 already applied (apply
  `jarvis-repeat-failure-counter.patch` first — this one touches the same
  two files and expects that patch's context, not the raw zip baseline's)
- `jarvis-shape-historical-tool-runs.patch` — enhancement #8 (`ai_client.py`:
  `_tool_runs_note()` gains a `verbosity` param, defaulting to `"full"`,
  and re-shapes each run's cached result via
  `tool_result_shaping.shape_result()` before serializing it; its one call
  site in the provider-attempt loop passes
  `verbosity_ref[0] if verbosity_ref else "full"`); diffed against the tree
  with enhancements #2/#5/#6 already applied, since it touches the same
  `ai_client.py` those patches already modified.
- `jarvis-recap-tool-calls.patch` — enhancement #9 (`conversations.py`: new
  `_compact_args()`/`_extras_recap_fragment()` helpers, wired into
  `conversation_messages()`'s older-exchange recap-line construction; no
  `ai_client.py` change needed since `_extras_from_runs()` already produces
  what this reads); applies independently of every `ai_client.py`-touching
  patch above, since it only touches `conversations.py`.
- `jarvis-log-router-activation.patch` — enhancement #10 (`tool_router.py`:
  `RouteResult` gains a `.matches` slot — a list of `(group, tool_name,
  matched_phrase)` triples — populated in `route()`'s matching loop;
  `ai_client.py`: `ask()` gains an optional `on_route` callback fired right
  after `tool_router.route()` runs; `cli.py`: new `on_route` closure
  printing one `$ routed: <group> (matched "<phrase>" on <tool>)` line per
  matched group, wired into its `ai_client.ask(...)` call). The
  `tool_router.py` half is diffed against the tree with enhancements #1 and
  #3 already applied (both touched that file already); the `ai_client.py`
  half against the tree with #2/#5/#6/#8 already applied.
- `jarvis-provider-override.patch` — new `provider_override` param on
  `ai_client.ask()` (see "Provider/key failover" above) plus `cli.py`'s
  `_extract_provider_override()` and the `--provider NAME`/`--provider=NAME`
  CLI flag wired into `handle_ai_prompt`'s call site; diffed against the raw
  zip baseline (touches only `ai_client.py`/`cli.py`, neither modified by
  the `tool_router.py`-chain patches above, so it applies independently of
  those — but diff against the *current* tree if any other `ai_client.py`/
  `cli.py` patch has already landed first). Companion `tests/
  test_provider_override.py` (10 tests) ships as a plain new file alongside
  the patch, not folded into it.
- `jarvis-provider-override-cli-env.patch` — extends the override with a
  `JARVIS_PROVIDER_OVERRIDE` env-var fallback in `cli.py`'s
  `handle_ai_prompt` (consulted only when no argv `--provider` flag was
  given, so an explicit CLI flag still always wins) — this is what lets a
  caller with no argv to put a flag in (the web UI) still set the override.
  Diffed against the tree with `jarvis-provider-override.patch` already
  applied (touches the same `cli.py` region); apply that one first.
- `jarvis-provider-override-web-ui.patch` — the Ask panel's provider
  picker: new `GET /api/ai/providers` route in `server.js` (reads
  `ai_config.json` directly and hand-mirrors `_eligible_providers()`/
  `_sort_providers_by_priority()` in JS — enabled + has a real key, ordered
  by `provider_priority` — returning only `{name, model, type}`, never the
  actual keys); the `"ask"` WS handler forwards an optional `msg.provider`
  (validated `/^[A-Za-z0-9_-]{1,64}$/`) as `JARVIS_PROVIDER_OVERRIDE` in the
  spawned subprocess's env, same pattern as the existing
  `JARVIS_ALLOWED_TOOLS`; `index.html`/`style.css`/`app.js` add a "Provider:
  Auto" button next to Clear in the Ask panel header that opens a themed
  dropdown of currently-eligible providers (client-side-only choice —
  nothing persisted, resets to Auto on reload), threaded into both the
  normal ask and the "redo" WS payloads via `state.providerOverride`.
  Diffed against the raw zip baseline (touches only `web/*`, untouched by
  every patch above); depends on `jarvis-provider-override-cli-env.patch`
  (for the env var it relies on) but applies independently of it file-wise
  since there's no overlap.
- `jarvis-provider-order-picker.patch` — the web UI's picker goes from
  single-pick to multi-pick, ordered (`web/server.js`,
  `web/public/app.js`/`index.html`/`style.css`): `state.providerOverride`
  becomes an array built by click order instead of a single name-or-null;
  the picker menu grows numbered order badges and stays open across picks;
  `server.js`'s `"ask"` WS handler's provider-field regex widens from one
  name to up to 10 comma-separated names. This is the frontend for the
  list-form `provider_override`/`_order_providers_by_override()` that was
  already sitting in `ai_client.py` unused from the backend side — see
  "Provider/key failover"'s naming note above for why this isn't named
  `jarvis-provider-override-ranked.patch` despite that being what
  `ai_client.ask()`'s own docstring calls it. Diffed against the tree with
  `jarvis-provider-override-web-ui.patch` already applied (touches the same
  four files that patch introduced/modified).
- `jarvis-persist-console-dump.patch` — see "Console-dump persistence"
  above (`ai_client.py`: new `_split_console_dump()`, wired into the
  `append_exchange` call site) plus 4 new tests appended to
  `tests/test_enhancements.py` (now 30 tests total). Diffed against the raw
  zip baseline for `ai_client.py` (the `append_exchange` call site and the
  area right after `_is_tool_trace_reply`/`_TOOL_TRACE_LINE` weren't
  touched by any earlier `ai_client.py` patch) — but diff against the
  *current* tree instead if any of the enhancement/provider-override
  patches that also touch `ai_client.py` (#2/#5/#6/#8,
  `jarvis-provider-override.patch`) haven't landed yet in the tree you're
  patching.

If you're asked to build one of the outstanding ideas, or fix something new,
generate a fresh scoped patch the same way — don't fold it into an existing
patch file, and don't modify a delivered patch after the fact. **Exception
to "always diff against the zip baseline":** once a file has been patched
in the actual working tree (not just in this doc's bookkeeping), a new
patch touching that same file should be diffed against the *current*
tree state, or it won't apply — `git apply`/`patch` need matching context,
not just a description of what changed.