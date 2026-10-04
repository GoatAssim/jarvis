# AGENTS.md

Instructions for AI agents working in this repository. Read this first —
it captures constraints that aren't obvious from the code alone, and
following them will save you from re-deriving things or re-breaking
things that were already fixed once.

## Where things actually live

- Package root: `jarvis-cli/jarvis/*.py`. The repo root sits one level
  above `jarvis-cli/`.
- Tests live in `tests/` at the **repo root**, next to `jarvis-cli/`, not
  inside it.
- **`REPO_MAP.md` at the repo root is the current map.** Read it before
  grepping around: file layout, where every piece of state lives, the
  subsystems worth understanding first, and the full command surface.
- **`DOCUMENTATION/outdated/` is history, not documentation.** Everything
  in there is a point-in-time snapshot and is not maintained. Several of
  those files describe designs that were never built or bugs fixed long
  ago — including the old `jarvis-codebase-orientation*.md`, which used to
  serve the role `REPO_MAP.md` now serves. If one of them disagrees with
  `REPO_MAP.md`, `REPO_MAP.md` is right. Don't "restore" something from
  there without checking the code first.
- `DOCUMENTATION/` itself now holds only things that are still true:
  `PERSONAS_GUIDE.md` and the external `everything_sdk_*` / `yt-dlp-*`
  references.
- Enhancements #8–#10 (historical tool-run reshaping, recap-level tool-call
  summarization, router activation logging) landed without accompanying
  tests in `tests/test_enhancements.py` — they were verified with one-off
  inline snippets only. If you're touching any of `ai_client._tool_runs_note()`,
  `conversations._extras_recap_fragment()`, or `tool_router.RouteResult.matches`
  /`ai_client.ask()`'s `on_route` callback, add real coverage for it while
  you're in there rather than assuming it's already tested.

## Invariants — do not break these

- **Per-turn content never goes in the cached static prompt prefix.**
  `_system_prompt_parts()` returns (static, tail). Anything derived from
  the router's decision, the user's message, the conversation or the
  sender belongs in the tail. This has been broken twice — once by
  `pack_instructions_ctx`, once by the tools blurb's playnite/spotify
  flags — and both times the symptom was silent: the prompt still worked,
  the cache just never hit. `tests/test_prompt_cache.py` guards it.
- **The model may start, stop and inspect daemons; it may not create one.**
  Registering a daemon stores an argv Jarvis later runs unattended, and
  the model is the component most exposed to text written by other people
  (a chat guest, a fetched page, a file it was asked to read). There is
  deliberately no `daemon_add` tool. Same reasoning as `notify_owner`
  taking no recipient.
- **`send_dm` is the one tool that takes a recipient, so every limit on it is
  load-bearing.** It must stay owner-only (`JARVIS_CHANNEL_SENDER`), resolve
  only known contacts and never guess between two matches, stay in
  `TOOL_CONFIRM_REQUIRED`, keep its on-disk per-recipient rate limit, and
  refuse scheduled/unattended runs. `notify_owner` takes no recipient for the
  same reason; don't add one to it, and don't loosen `send_dm` to match.
  `tests/test_send_dm.py` pins each of these.
- **A chat guest's details never go in `memory.py`.** That store rides
  along in the owner's own prompts. Guest facts belong in
  `channels/people.py`, which is capped, keyed by platform id, and only
  ever surfaced back into that same person's conversation.
- **Anything that can be redelivered must be deduplicated.** Both chat
  platforms redeliver (Meta retries a webhook whose 200 was lost, Discord
  replays on a resumed session). `channels/dedupe.py` is the guard and it
  is on disk, not in memory, because a gateway restart is exactly when a
  redelivery happens.

- **Never touch `tool_safety.py`, confirmation prompts, `risk_review()`,
  or the AI-review gating inside `_make_tool_executor()`** while doing
  router/discovery/token-optimization work. It's a separate safety
  concern; changes there need their own explicit review, not a drive-by.
- **`MAX_TOOL_ROUNDS` in `ai_providers.py` stays at 5.** Never lower it to
  save tokens — optimize what's sent *per round* instead.
- `TOOLS`/`CORE_TOOL_SCHEMAS` stay fully loaded and locally executable at
  all times. Only what's **sent to the model** gets filtered by the
  router/discovery layer — never gate real tool execution behind it.
- Every provider's `tools_payload` is already rebuilt fresh each round.
  Don't reintroduce an ever-growing resent list.
- `jarvis` is a **brand-new OS process on every CLI invocation** (see
  `history.py`'s docstring). Nothing survives in memory between calls —
  persistence goes through `conversations.py` or `discovery_cache.py` on
  disk, never a module-level global.
- **There is no debug/verbose flag anywhere in `cli.py`.** All stderr trace
  output (`on_attempt`, `on_tool_call`, `on_tool_result`, `on_route`) is
  always-on, unconditional `print(..., file=sys.stderr)`. Don't assume one
  exists and gate new trace output behind it — either add real output
  unconditionally, matching the existing convention, or introduce the flag
  itself explicitly (and update every existing trace call site to respect
  it, not just the new one) if you actually want gating.

## Testing

No framework dependency — plain `assert` throughout (also valid as
pytest functions if pytest happens to be available). Each `test_*.py` is a
standalone script: it resolves its own imports off `Path(__file__)`, so it
runs correctly from any `cwd`, and it exits 0 on success / non-zero on
failure — no test runner was strictly required to run one file at a time.

**Running more than one file at a time used to mean copy-pasting a
hand-maintained list here — that list silently drifted (it named under a
quarter of the ~80 files actually in `tests/`) and nobody had to notice,
because nothing ever cross-checked it against the directory. Use
`tests/run_tests.py` instead — it discovers every `test_*.py` in `tests/`
by globbing the directory itself, so there is no list to fall out of
sync:**

    python3 tests/run_tests.py                     # run everything, one line per file
    python3 tests/run_tests.py --list               # see what it discovered, run nothing
    python3 tests/run_tests.py clipboard             # run only tests matching a name filter
    python3 tests/run_tests.py clipboard build_info   # multiple filters = OR
    python3 tests/run_tests.py -d clipboard_watch     # --detailed: full output, not just the summary line
    python3 tests/run_tests.py --fail-fast            # stop at the first failing file
    python3 tests/run_tests.py --jobs 8               # run in parallel (output stays in discovery order)
    python3 tests/run_tests.py --timeout 300          # override the per-test timeout (default 180s)

A filter matches by substring against the file's name with `test_`/`.py`
stripped, case-insensitive — `clipboard`, `clipboard_watch`, and
`test_clipboard_watch.py` all work. Exit code is 0 only if every selected
test passed, 1 if any failed, 2 if a filter matched nothing. Each test
runs as its own subprocess (`stdin` closed, so nothing can silently hang
waiting on input) with its own timeout, so one crashing or hanging file
can't take the rest of the run down with it. For a FAILING file the
summary shows the test's own `FAILED` lines, the final exception line and
its `N passed` count -- not the last line of output, which was often an
unrelated stderr notice (e.g. `[tools] Auto-discovered ...`) that hid the
real reason.

**Read the default timeout's own `--help` text before assuming a file is
hung** — several tests now involve real, unmocked sleeps.
`key_health.pace_key()` (K.3.6, `jarvis-cli/jarvis/key_health.py`) puts a
real `time.sleep()` floor (`DEFAULT_MIN_ROUND_INTERVAL = 3.0`s) between
two requests on the same key, called unconditionally from every one of
`ai_providers.py`'s five adapters via `_pace_round()`. `test_key_health.py`
mocks `time.sleep` for its own direct tests of `pace_key`/`_pace_round`,
but most other test files that exercise multi-round adapter scenarios
(`test_finish_signal.py`, `test_streaming_other_adapters.py`, etc.) don't
mock `key_health` at all — they now pay the real 3-second floor on every
same-key round, which is why `test_finish_signal.py` takes ~140s and
`test_streaming_other_adapters.py` takes ~65s where they used to be near-
instant. Those same unmocked tests also read/write the real
`~/.jarvis/key_health.json` on whatever machine runs them, rather than an
isolated temp copy — worth fixing (give the whole suite a shared fixture
that redirects `key_health.HEALTH_FILE`, the way `test_key_health.py`'s
own `_reset()` does just for itself) before this gets worse as more
adapter-loop tests are added. Set `min_round_interval_seconds` to `0` on a
provider block, or pass `--timeout` generously, in the meantime.

Front-end logic that's pure enough to run outside a browser gets a plain
Node script instead, same no-framework convention (the first two slice the real
function straight out of `web/public/app.js` by source range, so it can't
silently drift out of sync with a copy; `verify_daemons_panel.js` instead
loads all of `web/public/daemons.js` into a bare `window` with `vm` and calls
its exposed `JarvisDaemons._pure` helpers (`verify_daemons_console.js` does the same for the console
line classifier). `npm install marked` inside
`tests/` first for `verify_math_rendering.js`, the one that needs it to
actually render Markdown):

    node tests/verify_math_rendering.js
    node tests/verify_ask_trace_replay.js
    node tests/verify_daemons_panel.js
    node tests/verify_daemons_console.js   # the console line classifier (H.1.7)
    node tests/verify_l9_sequence_bar.js   # L.9: sequence bar reachable; needs `playwright` + Chromium, prints SKIP without them
    python3 tests/verify_l11_daemon_categories.py # L.11: daemon categories in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    python3 tests/verify_l13_favorite_daemons.py  # L.13: favorite daemons in a real browser; needs `playwright` (Python) + Chromium, prints SKIP without them
    node tests/verify_slash_palette.js      # no npm install; runs the real palette
                                            # against a fake DOM + fake JarvisHost
    node tests/verify_panel_open_guard.js   # I-B18(c): openScriptPanel() says so when a panel's script didn't load; no npm install
    python3 tests/verify_panel_open_guard.py  # same, in a real browser with daemons.js blocked; needs `playwright` (Python) + Chromium, prints SKIP without them

Two things that will waste your time if nobody tells you:

- **Redirect `HOME` to a temp dir BEFORE importing any jarvis module** in a
  test that touches `~/.jarvis`. Most modules resolve `Path.home()` at
  import time, so setting it afterwards has no effect and your test will
  read and write the developer's real store.
  `tests/test_workspace.py` shows the pattern.
- **Define test functions above the runner block at the bottom of the
  file.** The runner reads `globals()` when it executes, so a test appended
  after it is silently never run — and "N passed" still prints, which is
  how you don't notice.

Any new test file's `sys.path` setup must point at `jarvis-cli/`
(`Path(__file__).resolve().parent.parent / "jarvis-cli"`), or imports
fail with `ModuleNotFoundError: No module named 'jarvis'`.

Order of operations for a change:
1. `python3 -m compileall jarvis/<changed_files>.py -q` — catches syntax
   errors immediately.
2. A short inline snippet against the **exact repro** that prompted the
   change (import the module, call the changed function directly) — no
   API keys or live model needed.
3. A quick regression check with 1–2 adjacent phrasings/cases, so the fix
   doesn't overcorrect onto something that should still work.
4. Once a change has real regression risk (not a one-off repro), add a
   permanent test to `tests/test_enhancements.py` (or a new
   `tests/test_<topic>.py`, same no-dependency pattern) instead of
   leaving it as a throwaway snippet.
5. If `tests/interactive_inspector.py` exists, use it for a human-
   readable "what would actually happen for this message" view instead
   of hand-reconstructing the router/schema logic:
       python3 tests/interactive_inspector.py              # REPL
       python3 tests/interactive_inspector.py "a message"  # one-shot
       python3 tests/interactive_inspector.py --examples    # batch

**If you change the router's scoring/trim algorithm, or `ai_client.ask()`'s
active-schema construction, update the mirrored copy of that logic inside
`tests/interactive_inspector.py` in the same change.** It cross-checks
its mirror against the real code and prints `MIRROR DRIFT` on disagreement
— that's a safety net for catching a missed update, not a substitute for
making one. Run the `--examples` batch afterward and confirm no drift
warnings. Note: `tool_router.RouteResult` now carries a real `.matches`
field (list of `(group, tool_name, matched_phrase)` triples, populated by
`route()` itself) — the inspector's `_explain_keyword_matches()` still
re-derives this by mirroring rather than reading `route.matches` directly,
so there are technically two sources of per-phrase match detail now. Worth
collapsing to one (have the inspector just read `route.matches`) next time
you're touching that file, but it hasn't been done yet — don't assume
they've been unified just because `route.matches` exists.

## Test Checklist — adding a tool means updating it

The web console has **Menu → Test Checklist**: every tool Jarvis can call, how
to test it (prompts for Ask, arguments for a Debug direct-run), what a pass
looks like, and a place to record what works and what doesn't. An entry has
**two possible homes**:

- **Shipped:** `web/public/test-checklist-data.js`, for every tool that ships
  with jarvis.
- **The tool's own module:** a module-level `TEST_CHECKLIST` dict (tool name ->
  entry, the same shape, `group` optional and defaulting to `TOOL_GROUP`) —
  one entry per tool, so a module with several tools gives each its own —
  plus `TEST_CHECKLIST_GROUP` if the module invents a brand-new `TOOL_GROUP`
  (or more than one — see below). This is the **only** option for a user's own
  tool in `~/.jarvis/tools/`, which can never be in a file that ships with the
  app. `actions/_template.py` section 8 documents it; every Tool Manager
  template carries an example. Discovery reads it, `jarvis tools-list` (GET
  `/api/tools`) carries it, and the panel merges it in. Use ONE home per tool —
  the coverage test fails if a tool has an entry in both.

What counts as a well-formed entry is defined once, in
`jarvis-cli/jarvis/checklist_schema.py`, and checked by the same code for both
homes. A malformed module-supplied entry is dropped and logged
(`[checklist] file.py: ...`) — the tool itself still loads — and the Custom
Tools editor's Check button reports it; the coverage test then fails for a
shipped module because the tool is left with no entry.

**Whenever you add, rename, remove or change the behaviour of a tool, you MUST
update that tool's entry — in `web/public/test-checklist-data.js`, or in its own
module's `TEST_CHECKLIST` if that is where its entry lives — in the same
change.** This is not optional and not a follow-up. A tool change without its
checklist entry is an incomplete change, the same as a tool with no schema.

For each tool the entry needs:

- `group` — one of the ids in the file's `"groups"` list (match the group in
  `tool_registry.TOOL_GROUPS`; add a group there too if you added one). In a
  module's own `TEST_CHECKLIST`, leave it out and it follows the module's
  `TOOL_GROUP` — or, if the module's tools split across more than one
  logical category, set it explicitly to one of its own declared group ids
  (see `TEST_CHECKLIST_GROUP`'s two shapes just below).
- `does` — one line on what it's for.
- `steps` — at least one, ideally two or three: an `{"ask": "...", "expect":
  "..."}` prompt to type into Ask, and/or a `{"run": {...args...}, "expect":
  "..."}` direct run from Debug (skips the model). Write `expect` as what a
  pass looks like, concretely. Use `<angle brackets>` for things the tester
  must fill in — the UI highlights them.
- `needs` / `os` / `care` / `watch` when they apply: prerequisites (accounts,
  installed programs, plugins), Windows-only, side effects worth warning about
  (writes files, sends messages, starts processes, changes system state), and
  known gotchas or invariants worth checking while testing.

`TEST_CHECKLIST_GROUP` has two shapes. The common one, `{"label": ...,
"blurb": ...}`, names one section — the panel's home for the module's whole
`TOOL_GROUP` — and is what most modules need (one module, one category). A
module whose tools genuinely split across more than one logical category can
instead give it `{group_id: {"label": ..., "blurb": ...}, ...}`, one entry
per section, and tag each `TEST_CHECKLIST` entry's own `group` with the id of
the section it belongs in (an id from that dict, or the module's `TOOL_GROUP`
itself, which is always a valid target whether or not it also has its own
meta there). The two shapes are told apart by their keys alone — a plain
`{"label", "blurb"}` dict is the single-section shape; anything else is read
as `{group_id: meta, ...}` — so a module never needs to say which one it's
using. `actions/_template.py` section 8 works through both shapes with a
worked two-tool, two-group example.

Renamed a tool? Rename its key. Removed one? Delete its entry. If you changed
what a tool does, fix the affected `does` / `steps` / `expect` text — editing a
step's text automatically un-ticks it in testers' browsers, which is what you
want.

Do NOT put test results (status, notes, ticks) in that file. Results live only
in the tester's browser (localStorage); nothing about the checklist is ever
written by the CLI or stored under `~/.jarvis`. The panel is purely front end.

What happens if an entry is missing from both homes: the tool still appears in
the menu (the panel reads the live catalogue from `/api/tools`), but only as a
bare name marked **NO CHECKLIST**, with no details, and the Overview's Coverage
section names it. `tests/test_checklist_coverage.py` fails on this too for any
tool that ships with jarvis, so you'll hear about it before the tester does.

The data file is strict JSON between its `JSON-BEGIN` / `JSON-END` markers
(double quotes, no trailing commas, no comments) because both the browser and
that test parse it. A module's `TEST_CHECKLIST` is ordinary Python, but its
values must be plain JSON types (it is sent to the browser as JSON).

## The `/` command palette - adding a CLI subcommand or a Menu panel means updating its registry

The Ask box's `/` palette (`web/public/slash-commands-data.js` +
`slash-palette.js`) is a map of **every** name `jarvis-cli` reserves. When you add,
rename, or remove a subcommand:

1. Add/remove it in `jarvis-cli/jarvis/reserved_names.py` (already required).
2. Put the same name in `slash-commands-data.js` in exactly one place:
   - a verb's `covers` list (that verb is how a user reaches it), or
   - `passthrough` if it runs one-shot and headless - give it a `summary`, a
     `usage` string and a `riskTier`. This table is *also* the server allowlist
     for `POST /api/slash/run` (`server.js` reads this file), so adding a name
     here is what makes it runnable from a chat box, and nothing else is;
   - `notExposed`, with a one-line reason, **only for a technical reason**: it
     never exits (a supervisor), it needs a terminal or the microphone, it is an
     internal hook, or it is a syntax token. Destructive is not a technical reason:
     mark it `dangerous` in `passthrough` (owner decision D-I4 - flagged, not hidden).
   `tests/test_slash_coverage.py` fails if a reserved name is in none of the three,
   in two, or in the registry but not in `reserved_names.py`.
3. Tier honestly: `safe` (read-only or trivially reversible), `caution` (changes
   state but scoped and recoverable), `dangerous` (bulk-destructive, irreversible,
   or broad blast radius). `dangerous` passthrough commands are confirmed
   client-side before the request is made.
4. A new *verb* means a handler in `slash-palette.js`, a `riskTier` that matches
   what a typo costs, `confirm` if it is destructive or interrupts something
   running, `whileReplying: false` if it must not fire mid-reply, and a `preview`
   line. Keep "first Enter completes, second runs" for anything that is not a
   plain `safe`, non-Chat verb with nothing required to fill in
   (`runsOnFirstEnter()`).
5. Add the case to `tests/verify_slash_palette.js`, and the manual steps to
   `DOCUMENTATION/COMMAND_PALETTE_TESTING.md`.
6. Passthrough arguments are an argv array, never a shell string. Do not build a
   command line by string concatenation anywhere on this path.

Adding a **Menu panel** (a new `menu-item-*` in `index.html`) is the same rule:
give the verb that opens it a `menu` field naming that id, and an opener in
`JarvisHost.openPanel()` (bottom of `app.js`). `test_slash_coverage.py` fails until
every `menu-item-*` is claimed by exactly one verb.

The parser lives in one place. Do not add another `/`-prefix regex to the ask-form
submit handler in `app.js`: register a verb instead.

## Categories (L.11 daemons, L.14 commands)

A daemon's `categories` is a list of labels - not a kind, and not the `builtin` flag, which is also a
lock and stays separate. Empty means "Undefined" in the panel. The name rules live twice and must
agree: `jarvis-cli/jarvis/categories.py` and `web/public/category-input.js`;
`tests/test_categories.py` runs one corpus through both. Vocabularies are per surface (a daemon's
suggestions never come from commands). A name reaches the CLI as `--category NAME`, and the flag
parser reads any following `--...` token as a flag, so `server.js` refuses names starting with `--`
(`daemonCategoryArgs`). Writes are strict (refused with a reason, nothing saved); reads of a
hand-edited registry are lenient. L.14 should reuse both files rather than copy them.

## Patch conventions

Changes ship as scoped `diff -u` patches, one concept per patch:

- Name each patch for what it does (`jarvis-<feature-or-fix>.patch`), not
  generically.
- Diff against the zip baseline you were handed — **unless** a file
  you're touching has already been patched in the working tree, in which
  case diff against the *current* tree state instead, or it won't apply
  (`patch`/`git apply` need matching context, not a description).
- Don't fold a new change into an existing delivered patch, and don't
  modify a delivered patch after the fact. If a later patch depends on an
  earlier one touching the same file, say so explicitly (which patch must
  be applied first).
- Before handing a patch over: verify it actually applies cleanly against
  its stated base, and that the full test suite still passes afterward.

## Style already in place

- Inline comments explain *why*, not just what — match that density.
- Router/discovery/schema-shaping changes must be backward compatible by
  default: a no-op for the common case, behavior change only for the
  specific edge case being targeted.
- Prefer additive, opt-in data-shape changes (e.g. a value that can be a
  plain type or a richer dict, unwrapped via `isinstance()`) over
  changing an existing field's meaning — so old entries never need mass
  editing.
