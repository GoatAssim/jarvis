# Jarvis Token-Optimization System — Codebase Orientation

Read this before grepping around. It's a map of what's already true in this
codebase, written by an AI that already did the exploration — the goal is you
spend your tool calls on the *new* task, not re-discovering any of this.

**Lineage:** the zip you've been handed (`jarvis-counterreword_N.zip`) is the
`counterreword` branch state — original Jarvis + the user's own
`counterreword.patch` + Phases 1–9 of a token-optimization plan, all already
merged in. It is NOT raw/unoptimized Jarvis. On top of that baseline, four
bugfix patches and three enhancement patches (see "Outstanding ideas" and
"Patches delivered so far" below) have since been delivered. If you're given
a fresh zip, diff it against what's described here before assuming anything
is still broken or still just a design — most of what used to be wrong has
already been fixed, and 3 of the 8 originally-outstanding enhancement ideas
are already built.

Package root: `jarvis-counterreword/jarvis-cli/jarvis/*.py`. All paths below
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
| `tool_router.py` | Local keyword router, zero model cost | `MIN_SCORE=5`, `ROUTER_MAX_GROUPS=2`, `ROUTER_GROUP_MARGIN=10` (weighted multi-group trimming — enhancement #1), `RouteResult` (`.tools`/`.groups`/`.confident`), `route(user_text)` |
| `tool_registry.py` | Static data: which tools exist in which group, what keywords activate them, per-group workflow guidance | `TOOL_GROUPS`, `TOOL_KEYWORDS` (values may be a plain int weight or `{"weight", "not_with"}` — enhancement #3), `TOOL_PACK_INSTRUCTIONS`, `TOOL_INDEX`, `group_of()`, `tools_in_group()`, `keywords_for()`, `keyword_weight()`, `keyword_exclusions()`, `pack_instruction()` |
| `discovery_cache.py` | Small on-disk, cross-process cache of recent `search_tools`/`search_commands` hits (enhancement #2) — new file, no prior counterpart | `CACHE_FILE=~/.jarvis/discovery_cache.json`, `TTL_SECONDS=300`, `MAX_ENTRIES=20`, `cache_lookup(query, kind)`, `cache_store(query, kind, found)` |
| `tools.py` | Full tool schema catalog + schema-shrinking helpers + the `search_tools` discovery tool | `CORE_TOOL_SCHEMAS`, `TOOL_SCHEMAS` (the full catalog), `DISCOVERY_TOOL_SCHEMAS` (just `search_tools`), `DISCOVERY_AND_COMMANDS_SCHEMAS` (`search_tools` + `search_commands`, added later — see bug log), `schemas_for_tools()`, `compact_schemas_for_prompt()`, `name_only_schemas_for_prompt()`, `tool_search_tools()`, `_SEARCH_TOOLS_CAP=8` |
| `command_tools.py` | Saved-command system: search/run/create/update user-defined command routines | `COMMAND_TOOL_SCHEMAS`, `COMMAND_TOOLS`, `tool_search_commands()`, `_resolve_command()` (returns `needs_clarification: True` on a miss), `resolved_run_for_review()`, `command_call_requires_confirmation/ai_review()`, `_LIST_ALL_CAP=40` |
| `ai_client.py` | The orchestrator: `ask()`, message/prompt assembly, the shared tool executor, provider/key failover loop | `ask()` (now also cache-pre-seeds `active_schemas` from `discovery_cache` on a not-confident route — enhancement #2), `_build_messages()`, `_make_tool_executor()` (+ its `discover_sink` hook and `cache_query` param, which feeds `discovery_cache.cache_store()` on a `search_tools`/`search_commands` hit), `_FAILED_LOOKUP_THRESHOLD=3`, `PROMPT_MODE_DEFS` (capacity modes), `_prompt_profile()`, `_eligible_providers()`, `AskResult` |
| `ai_providers.py` | Per-provider API adapters + the tool-round loop | `MAX_TOOL_ROUNDS=5` (**never lower this to save tokens** — optimize what's sent per round instead), `ADAPTERS`, `call_gemini()`, `call_openai_compatible()`, each with its own `_tools_payload()` rebuilt fresh every round |
| `tool_result_shaping.py` | Trims what a tool call **returns**, after execution (the output-side counterpart to `tools.py`'s schema shrinking) | `TOOL_RESULT_SPECS` (opt-in allowlist — unlisted tools pass through unchanged), `shape_result(name, result, verbosity)` |
| `tool_safety.py` | Confirmation/AI-review gating, independent of token optimization — **do not touch for this work** | `requires_confirmation()`, `requires_ai_review()`, `DEFAULT_AI_REVIEW` |
| `conversations.py` | On-disk history, since each CLI call is a fresh process | `conversation_messages()` (recent turns + recap of older ones), `append_exchange()`, `other_conversations_context()` |
| `memory.py` | Cross-session memory | `prompt_context(compact, query, extra_texts)` — already relevance-scores against `query` and returns `""` on no match |
| `desktop_tools.py` | Mouse/keyboard/window schemas + implementations | `click`, `focus_window`, `list_windows`, `get_active_window`, `get_window_size`, `get_window_info` (**the last three overlap heavily** — see bug log) |
| `ocr_tools.py` | `click_on_text` — screenshot + local OCR + click, in one call, by visible label text |
| `cli.py` | Terminal trace output (the `↳ asking gemini (key n/N)…` / `$ <tool>` / `tokens: in=X out=Y` lines you see in logs) | the `on_attempt` print line, a tool-name→trace-verb dict (e.g. `"search_commands": "searching commands"`) |
| `tests/test_schemas_for_tools.py`, `tests/test_enhancements.py` | Automated tests — see "Testing pattern used in this project" below | run directly with `python3 tests/test_<name>.py`; no framework dependency |

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
   doesn't help there.

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
   adapter).

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
implementation designs (no code, when first written) for 8 further ideas.
**Three are now built** (see "Patches delivered so far" below) — the doc
itself is unchanged (still the original design), so treat items 1–3 there
as historical design rationale, not a to-do:

- ~~#1 weighted multi-group router confidence~~ — done, `tool_router.py`.
- ~~#2 cross-process discovery cache~~ — done, new `discovery_cache.py` +
  `ai_client.py`.
- ~~#3 exclusion keywords in `TOOL_KEYWORDS`~~ — done, `tool_registry.py` +
  `tool_router.py`.

Still outstanding, in the same doc: #5 a generalized (not tool/command-
specific) repeat-failure counter, #6 dict-keyed structural dedupe of the
active schema set, #8 shaping historical tool-run notes at *current*
verbosity rather than cached verbosity, #9 recap-level summarization of
prior tool calls (not just chat text), and #10 logging the router's
activation reason. Read the design doc's section for the relevant number
before building one of these rather than re-deriving the design.

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
- `tests/test_enhancements.py` — covers the three built enhancements:
  router score trimming (near-tie keeps both groups, a weak group beyond
  `ROUTER_GROUP_MARGIN` gets dropped, `ROUTER_MAX_GROUPS` caps even a
  within-margin third group), `discovery_cache.py` (round-trip, kind
  isolation, TTL expiry, the `MAX_ENTRIES` cap, corrupt-file handling —
  all isolated to a temp file, never touching a real `~/.jarvis`) and its
  wiring into `_make_tool_executor()`'s `cache_query` param, and the
  `TOOL_KEYWORDS` exclusion-keyword shape (`keyword_weight()`/
  `keyword_exclusions()`, and that an excluded phrase actually cancels a
  match while a plain phrase is unaffected).

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

If you're asked to build one of the outstanding ideas, or fix something new,
generate a fresh scoped patch the same way — don't fold it into an existing
patch file, and don't modify a delivered patch after the fact. **Exception
to "always diff against the zip baseline":** once a file has been patched
in the actual working tree (not just in this doc's bookkeeping), a new
patch touching that same file should be diffed against the *current*
tree state, or it won't apply — `git apply`/`patch` need matching context,
not just a description of what changed.
