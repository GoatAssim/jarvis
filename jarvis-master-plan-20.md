# Jarvis — Master Plan (current audit 2026-09-22; historical revisions preserved)

One document that merges everything: the issues/streaming plan, the clipboard
plan, the Playwright browser-control plan, the items the patch
(`jarvis-latest-changes.patch`, merged into repo 22) did **not** cover, the
Daemons/Backlog/Log search/Schedules UI rework (Part H), and the chat
code-block and `/` command-palette work with the bugs found on the way (Part I).

**Current-status rule:** the 2026-09-22 audit in Part 0 and Part K is the
authoritative snapshot for the supplied `jarvis-main(47).zip`. The long revision
log that follows is intentionally preserved as historical provenance; it can
contain intermediate statuses that were true for older zip baselines but are no
longer true today.

The current archive should therefore be read in this order: **Part 0 for the
current matrix, Part K for the actionable backlog, Parts A–J for the original
design/evidence, and the revision notes only to understand how a feature got
there.**

**Revision 2026-09-20 — adds Part F.** Two real failing turns (a `.env` token
fix through `code_agent`, and moving one PDF) were pulled apart from their raw
provider logs and cross-read against the source. Both ended with the requested
work **not done**, for reasons that are mostly small, independent and
fixable — a shell tool that isn't a shell, hidden files, a missing move tool,
and a round-budget that throws away the model's correct last call. Part F
records the evidence, the causes, suggested fixes, a suggested order, and the
decisions only the owner can make. No code was changed.

**Revision 2026-09-20b — F.4 and F.5 delivered.** The first two Tier 0a items
from F.15's order of work are done: `jarvis-run-shell-windows-args.patch`
(F.4) and `jarvis-list-dir-dotenv-visibility.patch` (F.5). Both are diffed
against the original zip baseline, apply in either order (they touch disjoint
regions of `code_agent.py`), and land with new permanent tests. Status
recorded in §0.0/§0.1b and inline in F.4/F.5 themselves; nothing else in Part
F was touched.

**Revision 2026-09-20c — F.2 delivered (items 1–2 only).** Tier 0a item 3:
`jarvis-unknown-tool-hint.patch`. `execute_tool`'s unknown-name path now
gives the same `did_you_mean`/`search_tools` hint `get_tool_schema` already
had, and the compact/ultra-compact system-prompt blurbs now say "never
invent a tool name". Item 3 of F.2's suggested fix (a separate, uncharged
discovery-call budget) is owner decision D1 — **not done**, changes what a
round is. Applies cleanly on top of the two F.4/F.5 patches; full relevant
test suite still green. Status recorded in §0.0/§0.1b, F.2, and F.15.

**Revision 2026-09-20d — F.6 delivered.** Tier 0a item 4:
`jarvis-code-agent-budget-and-result-shaping.patch`. The inner loop's system
prompt now states its real per-attempt round limit as an actual number
(with "edit now once the fix is evident" / "your last request must be
tool-less" guidance); every completed step gets a short outcome line
(`list_dir → 1 entry`, `run_shell → exit 1: ModuleNotFoundError...`) instead
of a bare tool name, collected into a new compact `log` field on
`tool_code_agent`'s result; `TOOL_RESULT_SPECS["code_agent"]` drops the bulky
raw `steps` telemetry at low verbosity and caps `last_error`, since `log` is
what a tight recap should read instead. Applies cleanly on top of the
F.4/F.5/F.2 patches (51-check new test file); full test suite still green
except the pre-existing, unrelated `_eligible_providers()` failure noted in
§0.1b. **Not done:** capturing "the agent's last words" beyond the existing
`last_error` string (would mean changing `ai_providers.AIResult` itself — a
shared-adapter-loop change, not something scoped to `code_agent.py`; see
F.6's own status note). **Not verified:** whether a real model actually
economizes its calls given the new prompt — no live model/network access in
this sandbox, same caveat as F.4's Windows box. Status recorded in
§0.0/§0.1b, F.6, and F.15.

**Revision 2026-09-20e — F.10 delivered (items 1–2 only).** Tier 0a item 5:
`jarvis-router-paste-confirmation.patch`. `conversations.last_assistant_reply`
+ `ai_client._strip_pasted_previous_reply` strip an exact, substantial paste
of Jarvis's own previous reply out of the text handed to `tool_router.route()`
— verified against the real Case 2b text that this alone fixes the observed
mis-route (`commands` matches, `channels`/`scheduling` don't). A short
confirmation ("yes plz run this...") that's also a confident-but-narrow
route now merges into a still-live sticky group instead of replacing it
(`_merged_sticky_groups_for_confirmation`, wired into `ask()` by mutating
the `RouteResult` in place so `_build_messages`'s pack-instructions and
`active_schemas` both see the merged view). `route_stickiness.py`'s
docstrings updated to stop claiming an unconditional "never merges" now
that `ask()` is a caller that deliberately does. **Not done:** item 3 (extra
weight for a message's last line) — that means touching
`tool_router.route()`'s scoring algorithm itself, which `AGENTS.md` requires
a matching update to `tests/interactive_inspector.py`'s mirrored copy for;
skipped since items 1–2 already fix the observed failure. **Not run by me
this round** — compiled and patch-verified (applies cleanly to a fresh
`jarvis-main_29_.zip` checkout) but the new test file
(`tests/test_router_paste_and_confirmation.py`, 22 cases) has not actually
been executed by me; run `python3 tests/test_router_paste_and_confirmation.py`
yourself. Status recorded in §0.0/§0.1b, F.10, and F.15.

**Revision 2026-09-20f — F.10 bug fix: `_looks_like_short_confirmation`
false-positived on real instructions.** Running the tests (owner-reported)
caught `test_looks_like_short_confirmation_negative_cases` failing on
`"please install ffmpeg for me"`: the original `_CONFIRMATION_RE` only
matched a leading *prefix* (`^(yes|...|please|...)\b`), and "please" is
both a genuine confirmation word and an ordinary way to start a brand-new,
unrelated instruction — so any "please <do something new>" message was
wrongly treated as a continuation of the previous sticky context instead
of a topic change. Fixed by anchoring the regex across the **whole**
message (`^...$`) instead of just its start: every word now has to be
either a bare filler (yes/plz/please/sure/ok/...) or part of one of a
small set of fixed confirmation phrases, with `run (it|this|that)` alone
allowed up to 3 trailing words (the real Case 2b shape, "run this custom
command") since that's the one place a confirmation legitimately names
its own object. Verified directly against both the original positive/
negative cases and two new adversarial ones (`"ok let's do the taxes
now"`, `"sure thing order me a pizza"`) — all now classify correctly.
Regenerated `jarvis-router-paste-confirmation.patch` against a fresh
`jarvis-main_29_.zip` baseline; reapplies and compiles cleanly. Added the
regression case to `tests/test_router_paste_and_confirmation.py` (now 22
cases) — **still not run by me**, only compiled; you already have the
harness, so re-run it yourself to confirm the fix.

**Revision 2026-09-20g — Part B (clipboard tool) delivered.** While F.3/
F.1/F.8 were in progress elsewhere, `jarvis-clipboard-tool.patch` was built
independently against the same `jarvis-main_30_.zip` baseline, touching only
`tools.py`/`tool_registry.py` (the same splice points `AUDIO_TOOL_SCHEMAS`
uses) plus one new module — no overlap with `tool_safety.py`, `code_agent.py`,
`ai_providers.py` or `tool_router.py`'s scoring, so it should apply cleanly
regardless of what order this lands relative to F.3/F.1/F.8. Delivers
`clipboard_get`/`clipboard_set`/`clipboard_clear`/`clipboard_wait_for_change`
in `clipboard_tools.py`, registered in a new `"clipboard"` `TOOL_GROUPS`
entry with its own `TOOL_KEYWORDS`/`TOOL_PACK_INSTRUCTIONS` (so it's routable
like every other group, per `tool_registry.py`'s consistency checks), and a
new `tests/test_clipboard_tools.py` (15 cases: result shaping, truncation,
the Linux multi-binary fallback including the no-display-server short
circuit, and the wait-for-change poll loop, all via monkeypatched
`_get`/`_set`/`subprocess.run` — no real clipboard needed). Verified:
`python3 -m compileall`, the new test file standalone, and the full
`tests/test_*.py` suite against a fresh patched checkout — all green except
the same pre-existing, unrelated `_eligible_providers()` failure in
`tests/test_subagents.py` noted in §0.1b (present on the unpatched baseline
too). **Not done in this revision (delivered next, see 2026-09-20h):**
the background `clipboard-watch` daemon/CLI subcommand. **Not verified:** the real per-OS backends
(`Get-Clipboard`/`Set-Clipboard`, `pbpaste`/`pbcopy`, `wl-paste`/`xclip`/
`xsel`) — no Windows/macOS box or a Linux display server in this sandbox,
same caveat as F.4's Windows box and F.5/F.6/F.10's live-model caveats.

**Revision 2026-09-20h — Part B's "Continuous watch" delivered, with one
deliberate deviation from the spec text.** `jarvis-clipboard-watch.patch`
(applies on top of `jarvis-clipboard-tool.patch`). The spec's step 3 called
for a `clipboard_watch_start`/`clipboard_watch_stop` **AI tool pair**
wrapping `daemons.add()`/`daemons.stop()`. That's not what got built,
because it directly conflicts with an existing invariant: `AGENTS.md` and
`workspace_tools.py`'s own docstring both say, in as many words, that there
is deliberately no `daemon_add`-shaped tool — registering a daemon stores
an argv Jarvis later runs unattended, and the model is the component most
exposed to injected text. A `clipboard_watch_start` tool is exactly that
shape, even scoped to one fixed command.

**What shipped instead, same end result:** `clipboard-watch` is registered
as a fourth **built-in** daemon in `daemons.py` (same footing as
scheduler/discord/instagram — a fixed, Jarvis-owned argv, pre-populated in
the registry, `argv` locked against edits, can't be removed). The model
gets it "for free" through the `daemon_start`/`daemon_stop`/`daemon_status`/
`list_daemons` tools it already has — zero new tool code — which is exactly
the "start, stop, inspect" half of the invariant the model is allowed. It
never gets a way to invent a daemon with an argv of its choosing. The
optional match pattern (plain regex data, never shelled out) lives in a
small `clipboard_watch_config.json`, set via a new **human-only** CLI
command, `jarvis clipboard-watch-config [--pattern REGEX|--clear-pattern]
[--poll-seconds N]` — not exposed as an AI tool, left as an open call
below rather than assumed safe by default.

Delivers: `clipboard_watch.py` (the `jarvis clipboard-watch` worker —
polls, notifies via `notifier.notify()` on change, filters by the
configured pattern if any, truncates long clipboard text the same way
`clipboard_get` does), `clipboard_cli.py` (dispatch for the worker command
and `clipboard-watch-config`, split out the same way `channels_cli.py` is),
the `daemons.py` built-in entry, and `tests/test_clipboard_watch.py` (14
cases: built-in registration and its immutability, absence of any
tool-based daemon control, config round-trip including bad-regex
rejection, the poll loop's change-detection/pattern-filtering/truncation,
and the CLI command). Verified the same way as the first clipboard patch:
compiles clean, both patches apply in sequence to a fresh
`jarvis-main_30_.zip` checkout, and the full `tests/test_*.py` suite is
green except the same pre-existing `_eligible_providers()` failure.

**New owner decision, D7 — should `clipboard-watch`'s pattern be settable
by the model?** Right now only a human can change what the watcher filters
on (`clipboard-watch-config`). Exposing that as an AI tool would be safe in
the narrow sense that it's just a regex string, never an argv — but it
wasn't shipped by default because "safe in the narrow sense" is exactly
the kind of judgment call this file's own D1–D6 pattern says shouldn't be
made silently. If wanted, it's a small, isolated addition on top of this
patch.

**Revision 2026-09-20i — Part C (browser control) delivered.**
`jarvis-browser-control.patch`: `browser_tools.py` with `browser_goto`,
`browser_click`, `browser_fill`, `browser_get_text`, `browser_screenshot`,
`browser_wait_for`, `browser_close`; a `jarvis browser-setup` CLI subcommand
(`cli.py`); a new `"browser"` `TOOL_GROUPS` entry with its own
`TOOL_KEYWORDS`/`TOOL_PACK_INSTRUCTIONS` (`tool_registry.py`); `browser_click`
and `browser_fill` added to `DEFAULT_CONFIRM_REQUIRED` (`tool_safety.py`); and
`tests/test_browser_tools.py` (28 checks). Two deliberate deviations from the
Part C text, both explained in the module docstring:

- **Session lifetime is "process lifetime", not a hook in `ai_client.ask()`.**
  Part C's registration step 5 asked for a per-ask context threaded through
  the tool loop with a `finally`. Every `jarvis` invocation (CLI and
  `web/server.js` alike) is already one OS process per ask, so the browser
  context is a lazily-opened module-level singleton closed by `atexit`. That
  lands at the same behaviour without touching `ai_client.py`. **Known gap:**
  a hard kill (SIGKILL, or the web Stop button's `taskkill /F` on Windows)
  skips `atexit`, so the Chromium child can briefly outlive its parent.
  Login state (cookies/local storage) lives in the on-disk persistent profile
  `~/.jarvis/browser-profile` and is unaffected. A v2 warm daemon with an
  idle timeout would close the gap; not built.
- **Playwright is never imported at module top level.** A missing install
  degrades every browser tool to `{"ok": false, "error": "…run: jarvis
  browser-setup"}` instead of breaking `tools.py`'s import chain.

Other behaviour worth knowing: `browser_goto` refuses `file:`, `javascript:`
and `data:` URLs outright (no confirmation path); elements are always described
in plain English and resolved role → label → text → literal-selector fallback;
`browser_screenshot` shows the image in the Jarvis UI and does **not** send it
to the model; headless is the default (`defaults.browser_headless` in
`ai_config.json` — not exposed to the model). **Not verified:** a real
Chromium run and, above all, that a real site's login survives two separate
`jarvis` invocations (the crux of the session-model decision). The tests use a
fake Page/Locator; set `JARVIS_TEST_REAL_BROWSER=1` to also run the opt-in
headless-Chromium check where Playwright and its browser are installed.

**Revision 2026-09-20j — F.3 (move / copy / rename / make-folder / delete)
delivered.** `jarvis-path-tools.patch`: `actions/path_tools.py` (auto-discovered,
`files` group) with `move_path`, `copy_path`, `rename_path`, `make_dir`,
`delete_path`, plus `tests/test_path_tools.py` (106 checks), a `REPO_MAP.md`
line, and an optional `trash = ["send2trash>=1.8"]` extra in `pyproject.toml`.
Deviations from F.3's suggested fix:

- **`tool_safety.py` was not modified.** F.3 (and owner decision D4) assumed
  the confirm-gate needed a protected-file change. The action-file mechanism
  already covers it: `TOOL_CONFIRM_REQUIRED` at the bottom of `path_tools.py`
  is folded into `DEFAULT_CONFIRM_REQUIRED` at import time, exactly as
  `code_agent.py` and `workspace_tools.py` do. All five tools are confirm-gated.
- **Argument names are `src` / `dest` / `path`, not `dst`.** `policy.py`'s
  `_paths_in()` only recognises a fixed list of argument names when it scores
  a call for "touches ~/.ssh" / "writes outside your usual folders"; `dest`
  is on it, `dst` is not.
- Nothing overwrites by accident: an existing destination is an error unless
  `overwrite=true`, and even then only a *file* is replaced, never a folder.
  Every mutating tool verifies afterwards and returns `{ok, from, to}`.
- Refused outright regardless of confirmation: a drive/filesystem root, the
  home folder itself, OS directories, and `~/.jarvis` (otherwise
  `copy_path(evil.json, ~/.jarvis/tool_safety.json, overwrite=true)` would let
  the model rewrite the confirm-gate's own config through the tool it gates).
- `delete_path` sends to the Recycle Bin/Trash via `send2trash` and refuses,
  touching nothing, if it isn't installed. There is no permanent-delete path.
- Router keywords carry `not_with` exclusions so "move the mouse"/"move the
  window" don't pull the `files` group in; tests cover Case 2a's exact
  message reaching these tools.

**Not verified:** real Windows behaviour (cross-drive moves, Recycle Bin) and
a live model actually choosing `move_path` for "move X to Y".

**Revision 2026-09-20k — F.1 + F.8 + F.11 (forced ending) delivered, in part.**
`jarvis-forced-ending.patch` (`ai_client.py`, `ai_providers.py`,
`tests/test_forced_ending.py` — 30 checks). When the round budget is spent, an
adapter no longer sends one more request full of native tool-call structures
with `tools` removed (which models kept imitating, and which every adapter
treated as a dead key). It hands off to `ai_providers._forced_ending()`:

1. **Grace round** (once per `ask()`, only when `RoundBudget(grace=True)`): the
   transcript is flattened to plain text, tools come **back**, and the model
   may make one essential call — through the normal `tool_executor`/confirm
   gate, never around it (up to `GRACE_MAX_CALLS` = 4 calls from that one
   response).
2. **Final answer**: the transcript is flattened again and sent with **no**
   tools, plus one shared "tools are gone" notice used by every adapter (F.1
   item 1). Calls/results become prose (`I ran X with {…}. Result of X: …`),
   so there is no structure to imitate.
3. If the model *still* wants a call, that is `KIND_BUDGET`: `ask()` **does not
   rotate keys** and ends the turn through a harness-written reply
   (`_forced_ending_reply`) listing what ran, what it was about to run, and
   "say go ahead" — never using the words tool/budget/exhausted/round (F.11).
   It also writes the exchange to the conversation and sets `degraded=True`.

Also added: `AIResult.kind`/`.pending` and a central `classify_failure()`
(`KIND_BUDGET/KEY/OVERLOAD/NETWORK/SHAPE/EMPTY/MALFORMED/REFUSED/OTHER`,
derived from the error text so the ~60 existing `AIResult(False, error=…)`
sites needed no edit); `repo_browser.` / `functions.` prefixes stripped from
tool names (gpt-oss); a call written into Groq's `reasoning` channel is
recovered (`_calls_from_reasoning`); a call written as plain text is recovered
(`_pending_from_text`); the recap/degraded reply now counts the new path tools
as mutating; thinking stays off and usage rounds keep their real numbers
inside a forced ending (`_log_local.forced`). All five adapters
(openai_compatible, Anthropic, Gemini, Cohere, Ollama) route through it.

**Owner decision D1 (F.16) — grace-call half taken, on by default.** The
patch implements the grace call and enables it in `ask()` unless
`defaults.grace_call` is set to `false` in `ai_config.json`. `MAX_TOOL_ROUNDS`
is unchanged. The other half of D1 — a separate, uncharged budget for
discovery calls (F.2 item 3) — is still **not done**. Confirm that
default-on is what you want; it is a one-line switch.

**Not done / not verified:**
- Only two kinds change behaviour today (`KIND_BUDGET` never rotates,
  `KIND_SHAPE` was already handled). `KEY`, `NETWORK` etc. still rotate as
  before; F.8 item 3's "a network failure on a `base_url` skips its sibling
  providers" and F.9's key-health memory were **not** built at this revision (both done later — rev. n).
- F.8 item 2 (Gemini `functionCallingConfig.mode = "NONE"`) was deliberately
  replaced by the flatten-and-resend design; it was never needed.
- F.11's "pending proposed action per conversation, so a bare *yes* executes
  it" is not built; the reply just tells the user to say "go ahead".
- **Tests use canned HTTP responses only.** The two reasoning-channel shapes in
  `_calls_from_reasoning` are educated guesses from the plan's description
  plus gpt-oss's harmony syntax (the raw log wasn't available); a wrong guess
  is harmless because only names in the offered tool list are honoured. The
  F.17 replay fixtures were not built, so the real Case 1/2b logs have not
  been replayed.
- F.7 (failover keeps `tool_history`, fix the recap) was untouched at this revision (done later — rev. n).

**Revision 2026-09-20l — Test Checklist (new web-console panel).**
`jarvis-test-checklist.patch`: **Menu → Test Checklist** lists every tool Jarvis
can call with how to test it (Ask prompts, Debug direct-run arguments, what a
pass looks like, prerequisites, side-effect warnings), and lets a tester record
a verdict per tool (untested / working / partial / not as intended / bug /
blocked), tick steps and add notes. See Part G for details. It is purely front
end: no server route and no CLI command; results live in the browser's
localStorage (Export/Import JSON), and the only request is the read-only
`GET /api/tools` Debug already uses. The catalogue is
`web/public/test-checklist-data.js` (strict JSON between `JSON-BEGIN`/
`JSON-END`). New files: `test-checklist.js`, `test-checklist.css`,
`test-checklist-data.js`, `tests/test_checklist_coverage.py`; touched:
`index.html`, `app.js`, `web/README.md`, `REPO_MAP.md`, and **`AGENTS.md` gains
a standing rule: adding, renaming, removing or changing a tool means updating
its checklist entry in the same change.**

**Revision 2026-09-20m — the six patches merged into one tree
(`jarvis-main_31_.zip`).** Applied to `jarvis-main_30_.zip` in this order:
clipboard-tool → clipboard-watch → path-tools → forced-ending → test-checklist
→ browser-control. Every patch applied cleanly on its own against the
baseline, and five of six applied in sequence with no edits. Notes from the
merge itself:

- **One real conflict, resolved by hand:** `jarvis-browser-control.patch` and
  `jarvis-clipboard-tool.patch` both rewrite the same single-line `if name in
  COMMAND_TOOLS or … or name in ("search_tools", "get_tool_schema"):` dispatch
  chain in `tools.py::execute_tool` (hunk at ~line 1076). The browser hunk was
  rejected; the merge appends `or name in BROWSER_TOOLS` to the chain that
  already contains `CLIPBOARD_TOOLS`. **If you ever add a third tool group this
  way, the same one-line collision will recur** — consider replacing that chain
  with a single combined set or a `TOOLS`-membership check.
- **One gap, fixed in the merge:** the test-checklist patch was built before
  the other patches' tools existed, so `tests/test_checklist_coverage.py`
  failed with sixteen tools missing an entry (`browser_*` ×7, `clipboard_*` ×4,
  `copy_path`, `delete_path`, `make_dir`, `move_path`, `rename_path`). The merge
  adds a checklist entry for each (`web/public/test-checklist-data.js`), plus
  two new groups, **Clipboard** and **Browser control**. Those entries are
  first drafts written from each tool's schema and tests — a human should read
  them once and tighten the `expect` text after actually running them. This is
  exactly the failure mode AGENTS.md's new rule exists to prevent.
- **Verification:** the whole tree compiles; the six new test files pass on
  the merged tree — clipboard-tools 15/15, clipboard-watch 14/14,
  browser-tools 28/28, path-tools 106/0, forced-ending 30/0, checklist
  coverage 5/5 — and the rest of `tests/test_*.py` still passes except the
  same pre-existing, unrelated `test_subagents.py`
  (`_eligible_providers()` `None` bug, see §0.1b), which also fails on the
  untouched `jarvis-main_30_` baseline.
- **Not done in the merge:** `REPO_MAP.md` has no entries for
  `clipboard_tools.py`, `clipboard_watch.py`, `clipboard_cli.py` or
  `browser_tools.py` (only `path_tools.py` and the checklist files were
  mapped by their patches).

**Revision 2026-09-20n — F.7, F.9, F.10 (item 3 + highlight) and F.12
(log labels) delivered, merged into `jarvis-main_32_.zip`.** Four patches,
applied in this order on top of `jarvis-main_31_`'s merged tree:
`jarvis-failover-transcript.patch` (F.7), `jarvis-key-health.patch` (F.9,
plus F.8 item 3), `jarvis-log-labels.patch` (F.12, log mislabelling only),
`jarvis-router-last-line-highlight.patch` (F.10 item 3 + the highlight-quote
path). Confirmed independently: reconstructing this tree from
`jarvis-main_31_.zip` plus the four patches (`jarvis-f-steps-all.patch`)
byte-for-byte matches the delivered `jarvis-main_32_.zip` — nothing was
hand-edited after the fact — and on that tree the full `tests/test_*.py`
suite passes except the same pre-existing `test_subagents.py`
(`_eligible_providers()`) failure, with no `MIRROR DRIFT` from
`tests/interactive_inspector.py --examples`.

- **F.7 — failover keeps the transcript.** The next key or provider now
  continues from the failed attempt's own accumulated tool-call history
  instead of a ~1600-character recap stitched into a second "user" message.
  Oversized tool results are cut in the middle so the *end* of each survives
  (where an error or exit code usually lives), not just the head. `code_agent`,
  `run_shell` and `edit_file` count as "something real ran" for the recap's
  own bookkeeping only — not for the forced-ending path's "was an action
  already completed" check (F.1/F.8), which stays scoped to what it was
  before. The recap no longer tells the next key "you MUST call the real
  tool", which was fighting the model's own judgment about whether one was
  still needed. `tests/test_failover_transcript.py`, 18 cases.
- **F.9 — key health and pacing (in part).** New `key_health.py`:
  `~/.jarvis/key_health.json` stores per-key cooldowns (hashed key ids, never
  raw keys); a 429 parks the key for the delay the provider actually stated,
  surfaced back to the user as `[retry in Ns]`; a 503 moves the whole
  *provider* to the back of the list for ~45 s (model-wide condition, another
  key on it rarely helps) and skips that provider's remaining keys for the
  current attempt; a refused connection skips sibling providers sharing the
  same host (e.g. a second Ollama entry) for the rest of that ask. Cooling
  keys are never removed, only tried last, soonest-to-recover first. Every
  ask now starts on the last key that actually worked
  (`key_health.order_keys`/`record_success`). `code_agent`'s inner loop
  starts on a *different* key than the outer ask via `spread_keys()` (the
  "give it a different key" half of F.9 item 3 — see this revision's own
  note on the "pacing" half, still open) and now records which keys it
  burned so the outer ask doesn't retry them. `doctor` gained checks for an
  unreachable Ollama and for keys currently cooling down. `jarvis-run-shell-diagnosis.patch`
  (below) independently confirmed `doctor.py`'s Ollama-unreachable check and
  `ai_client.py`'s same-host skip are both real, not just described.
  `tests/test_key_health.py`, 36 cases. **Not done:** waiting on a stated 429
  delay rather than rotating immediately is owner decision D5, still open;
  true per-round pacing of the inner loop (rather than just isolating its
  key) would mean touching the shared adapter round loop in
  `ai_providers.py` across all five adapters — out of scope for this small,
  independent item, same reasoning as F.6's "last words" follow-up. F.9's own
  suggested-fix item 3 says "pace the inner loop, **or** give it a different
  key"; the "or" is satisfied by `spread_keys()` alone.
- **F.12 — log mislabelling only.** A request made *to a different host*
  partway through an attempt — the Groq risk-review call fired while the
  running attempt is on a Gemini key, say — is now logged under that other
  host instead of being mislabelled with the active attempt's own key/provider
  label. Done without touching `risk_review()` itself, which `AGENTS.md`
  protects. **Not done, on purpose:** confirm churn (a read-only allow-list
  for `run_shell` confirms) is owner decision D6 and touches confirm gating,
  which is separately protected — explicitly flagged in F.12's own text as
  needing its own reviewed change, never bundled here.
  `tests/test_log_labels.py`, 4 cases.
- **F.10 — item 3 and the highlight-quote path.** The last line of a
  multi-paragraph message (3+ lines, 200+ characters) now gets a small
  routing bonus (+2) — completing the item skipped in the first
  `jarvis-router-paste-confirmation.patch` pass. Separately, a
  highlighted-excerpt prompt (the `web/server.js` path the first pass
  explicitly left untouched — see 0.1c) is stripped back to the user's own
  words before routing, in Python, mirroring the JS wording rather than
  changing it — **a real coupling**: if `web/server.js`'s highlight-quote
  wording ever changes, this strip silently stops matching and the original
  bug returns with no test to catch it, since the test file can only exercise
  the Python side. `tests/interactive_inspector.py`'s router-scoring mirror
  was updated through a shared helper (`AGENTS.md`'s "update the mirror in
  the same change" rule), and the `--examples` batch shows no drift.
  `tests/test_router_last_line_and_highlight.py`, 11 cases, including a
  "before" case that reproduces the original bug against the *unstripped*
  text (the quoted "let me know" really did pull in `channels` and
  `scheduling`) so the fix is checked against a real regression, not just a
  clean-room example.

**Revision 2026-09-20o — F.4's "diagnosis text" follow-up delivered.**
`jarvis-run-shell-diagnosis.patch`, built independently on top of
`jarvis-main_32_.zip` (after revision n above). This was F.4's one
remaining suggested-fix bullet, left open since the original
`jarvis-run-shell-windows-args.patch`: `tool_diagnosis.diagnose()` only ever
saw a tool's name and result dict, never the command that produced it, so a
cmd.exe builtin's `WinError 2` (run without the F.4 wrapper — a ctypes
failure, or a builtin outside `_CMD_BUILTINS`) got the same generic
"install ffmpeg/git/tesseract" advice as a genuinely missing external
program.

- `_run_shell_impl` (`actions/code_agent.py`) now carries a `command` field
  — the ORIGINAL command string, not the `cmd /c`-wrapped argv — on every
  outcome: success, timeout, and the OSError branch a builtin actually hits.
- `tool_diagnosis.diagnose()` special-cases `run_shell` plus a WinError-2-
  shaped error: if the command's first word is in the same `_CMD_BUILTINS`
  set `_run_shell_impl` itself checks, it returns `"'dir' is a cmd.exe
  builtin, not a standalone program…"` and `"Run it as: cmd /c dir /a"`
  instead of the generic advice. Every other tool, and every other
  WinError 2 (a real missing program, say `ffmpeg`), is unaffected — checked
  explicitly by test, not just asserted.
- **A second call site, found while doing the first:** `code_agent`'s own
  inner loop (`_execute_step`'s `run_shell` branch) never goes through
  `ai_client`'s tool executor, which is the only place
  `tool_diagnosis.annotate()` was wired in — so the inner loop's own
  `run_shell` failures (the ones that actually appear in F.7's failover
  transcript and in a failed job's recap) never got the hint at all, only
  the standalone tool did. `_run_shell_outcome()` (the short line that
  becomes that step's `log` entry) now calls `diagnose()` directly for the
  same builtin case. Small, additive, no protected file touched, no owner
  decision needed.
- `web/public/test-checklist-data.js`'s `run_shell` entry updated per
  `AGENTS.md`'s Test Checklist rule (a `run` step added, `does` text
  mentions both call sites).
- `tests/test_run_shell_diagnosis.py`, 14 cases: the `command` field on
  every `_run_shell_impl` outcome (including that it stays the *original*
  line on the `cmd /c`-routed path, not the wrapped one), the builtin-vs-
  generic diagnosis split, a defensive case for a result with no `command`
  key at all, and the inner-loop outcome line for both the builtin and
  generic cases.
- Verified against the actual `jarvis-main_32_.zip`: applies cleanly,
  compiles, full suite green except the same pre-existing
  `test_subagents.py` failure, no inspector drift.

**Revision 2026-09-20p — §2 (Tesseract "on PATH" but OCR still fails):
diagnosis bug fixed, in `jarvis-main_36_.zip`.** `jarvis-ocr-diagnosis-fix.patch`
(already in the `_36_` tree; this revision documents it and re-checked it
there). §2 above guessed the cause was a stale PATH in the long-running
server; the code-side finding is different and now fixed: **the diagnosis
Jarvis attached to a failed OCR call was itself wrong.**
`tool_diagnosis.diagnose()` matched the words "tesseract"/"not found" anywhere
in the error, but `ocr_tools.py`'s error always carries a long install note
that names `tesseract`, `pytesseract` and `Pillow` whatever actually broke. So
Jarvis told the user to install things they already had, and when Tesseract
genuinely wasn't on PATH it *also* said the pip packages were missing and
told them to "reopen your terminal" — useless when the process that needs the
new PATH is the long-running web server.

- **What changed.** `read_screen` and `click_on_text` now go through their own
  classifier, `tool_diagnosis._diagnose_ocr()`, which decides from how the
  tool's **own** error message *starts* (`pytesseract/pillow not installed`,
  `tesseract binary not found on path`, `OCR failed:`), never from words
  buried in the install note. Everything else is untouched: other tools keep
  the generic matcher, and an unrecognised OCR error returns `None` (the raw
  error passes through) rather than a guess.
- **The four outcomes.**
  - *Not on PATH* → names only the Tesseract program. Windows fix: open a
    **new** terminal, run `where tesseract`; if it prints a path, fully
    restart the web server from that terminal; if nothing prints, run
    `winget install UB-Mannheim.TesseractOCR` or add
    `C:\Program Files\Tesseract-OCR` to PATH. (Other OSes: same with
    `which tesseract`.)
  - *Language data missing* (`OCR failed:` + tessdata/traineddata wording) →
    says Tesseract **was found** but can't load `eng.traineddata`, points at
    `TESSDATA_PREFIX`, and no longer says to install anything.
  - *Pip packages missing* → names only `pytesseract` and `Pillow`.
  - *Anything else that ran and failed* → "run `tesseract --version` and
    `tesseract --list-langs`" plus how to see which `tesseract` is first on
    PATH. A screen-capture failure (`screen capture failed:`) gets **no**
    Tesseract advice at all.
- **Tests.** `tests/test_ocr_diagnosis.py`, 11 checks, built from the *real*
  error strings captured from failing calls; they fail against the old
  matcher. A drift check builds the messages from `ocr_tools.py` itself, so
  rewording an OCR error prefix fails a test instead of silently disabling the
  classifier (the same "no shared source of truth" hazard as the other
  mirrors). Re-run on the `_36_` tree: 11/11.
- **Docs the patch touched.** Inline comments explaining why, a `REPO_MAP.md`
  entry for `tool_diagnosis.py`, and a `watch` note on both OCR tools in
  `web/public/test-checklist-data.js` (the AGENTS.md Test Checklist rule).
- **Not verified:** on a real Windows machine with a real Tesseract install,
  and that a real long-running server process actually picks up a fixed PATH
  after the restart described in §2.

**Revision 2026-09-20q — F.10's highlight-wording mirror gets a drift guard
(a deliberately tiny Part F item).** Built on the `_36_` tree. Revision n
delivered the F.10 highlight-quote fix by mirroring, in Python, the wrapper
sentence `web/server.js` puts around a highlighted excerpt, and flagged the
risk in the plan's own words: *if the JS wording ever changes, the strip
silently stops matching and the original bug returns, with no test to catch
it.* This closes exactly that gap and nothing else.

- New `tests/test_highlight_wrapper_drift.py` (6 checks). It reads the string
  literals the JS concatenates (`let prompt = text;` … `if (prompt.length`) out
  of `web/server.js`, rebuilds the exact prompt the server would send —
  including the empty-text case, where the JS substitutes its default ask —
  and runs it through `ai_client._strip_highlight_excerpt`. It asserts the
  user's own words come back out, that server.js's default ask equals
  `_HIGHLIGHT_DEFAULT_ASK`, and that the block still has the five literals the
  mirror expects; if the JS is restructured the test says so instead of
  guessing. **Mutation-checked:** rewording the sentence in a scratch copy of
  `server.js` makes 3 of the 6 checks fail; restored, 6/6 pass.
- **Comment-only edits** in the two places that need to know about each other:
  `ai_client.py` above `_HIGHLIGHT_WRAPPER` (it now names the test) and
  `web/server.js` above `let prompt = text;` (a three-line note saying the
  wrapper is mirrored and which test fails on a mismatch). No behaviour, no
  wording, no protected file touched; `node --check` and `compileall` clean.
- **Why this item:** the smallest thing left in Part F that needs no owner
  decision, no protected file, and no shared-adapter change — the remaining
  items are the F.2 project-content discovery-budget question,
  `router`/adapter loops, or F.6's last words (D5/D6/D7 and the F.17
  fixtures are done as of 2026-09-21).
- **Baseline for both revisions:** `jarvis-main_36_.zip`. Full
  `tests/test_*.py` on it, plus the new file, passes except the same
  pre-existing `test_subagents.py` failure (`_eligible_providers()` `None`
  bug, §0.1b). I only had `_36_`, not `_33_`–`_35_`, so anything that landed in
  those and isn't in revisions n–o isn't recorded here.

**Revision 2026-09-20r — adds Part I (code blocks with one-click copy, a rebuilt `/` command palette) and documents twelve bugs found while researching it.**
Built on `jarvis-main_39_.zip`; planning only, no code changed.

- Two bugs were **reproduced by running the code**, not just read. **I-B1:** `extractMath` runs on the raw reply, so a `$$`, a `\[`…`\]`, or a pair of `$` inside code swallows the closing fence or the inline-code backticks and merges or breaks code blocks. **I-B2:** `cli.py`, `web/server.js` and `commands_config.py` keep three different "reserved name" lists; a saved command named `think` is accepted and then silently shadowed by the built-in, and 30 built-in subcommands are on no list at all. Both are small, independent fixes worth doing before the rest of Part I (§0.4 step 1c).
- Ten more are findings from reading the source — the `/skillload` autocomplete, the copy path and the render path (I-B3–I-B12). Two of them (I-B9, I-B12) are explicitly marked as not verified in a browser.
- Housekeeping: Contents now lists Part H (it was missing) and Part I; Part 0's not-started list and suggested order mention Part I. Part H is still absent from Part 0's status lists — left as it was.

**Revision 2026-09-21 — D5, D6, D7 delivered; F.17 fixtures built and run against the real logs.**
Built and tested against the actual repo, not planning-only. See F.9, F.12,
F.16 and F.17 above for the full detail; summary:

- **D5** (F.9): wait ≤30 s on a 429's own stated retry delay before
  rotating. Found already coded in `ai_client.py` (owner unclear) but its
  own test never re-ran the existing suite, so it had silently broken
  `tests/test_key_health.py`'s two-asks test and made the suite sleep 26 s
  per run — fixed by adjusting that test's fixture, not the feature.
- **D6** (F.12): a fixed, exact allow-list (`dir`/`type`/`where`/`echo`,
  no shell metacharacters) skips confirm+ai_review for `run_shell`, as its
  own isolated check per `AGENTS.md`'s protected-confirm-gate rule.
- **D7** (Part B): `clipboard_watch_set_pattern` / `_get_pattern` tools,
  no confirm gate, calling the existing `clipboard_watch.set_pattern()`.
- **F.17**: the stub-HTTP replay harness described below was actually
  built and run against the two real logs. Case 1 passes (routing only —
  can't verify the on-disk edit in a Linux sandbox). Case 2b does **not**
  pass — it's recorded as a known, tracked gap rather than silently
  marked done, tied to a narrower question than D1 turned out to be (see
  F.16's D1 correction: the discovery-budget mechanism itself was already
  shipped, just scoped to catalog lookups, not `search_files`).
- **Also re-confirmed, not newly found:** the pre-existing
  `_eligible_providers()`/`providers_from_env()` `None`-vs-`None` bug
  (already noted in §0.1b as a decision, not a start-from-scratch item)
  still reproduces — `tests/test_subagents.py::test_malformed_pool_yields_nothing_not_main_key`
  crashes with `TypeError: 'NoneType' object is not iterable` rather than
  the empty list the test expects. Left alone here too — it touches the
  subagent key-isolation boundary, which deserves its own review, not a
  drive-by alongside D5/D6/D7.
- Full `tests/test_*.py`: 1529 passed, 0 failed, apart from the
  above pre-existing crash (unchanged by this revision, in either
  direction).

**Revision 2026-09-21b — Part A §6 and §5 delivered (§5 on Anthropic only).**
Patch: `phase-a-partA-56.patch` (notes: `phase-a-partA-56-NOTES.md`). Verified
by applying it with `patch -p1` to a fresh `jarvis-main_40_.zip` extraction.
Touches `ai_providers.py`, `ai_client.py`, `reasoning.py`, `cli.py`, plus two
new test files. Details are in §6 and §5 of Part A below; summary:

- **§6 — done, all five adapters.** The hardcoded "at most 2 thinking rounds
  per turn" is replaced by `reasoning.max_thinking_rounds(level)`
  (`_LEVELS` gains a `max_thinking_rounds` field): `low` → 2 (unchanged),
  `medium` → 4, `high` → uncapped. It lives in the shared `_apply_thinking`,
  so every adapter picks it up. **Caveat:** a separate, pre-existing
  mechanism (`reasoning.round_patch`'s `budget_for_round` /
  `MIN_USEFUL_BUDGET` floor) already zeroes `low`'s post-round-0 budget for
  Anthropic/Gemini (`1024 * 0.4 = 409`, under the `1024` floor). It was left
  alone, so `low` on Anthropic/Gemini behaves as before; the visible change is
  at `medium`/`high`, and at `low` on OpenAI-compatible-style providers.
- **§5 — Anthropic only.** `call_anthropic` now extracts text every round.
  Text that arrives alongside a `tool_use` block is (1) captured by the new
  `ai_providers.get_interim_text()` (per-attempt `{"round", "text"}` list,
  reset in `set_thinking()`), (2) fired live before the tools run through a
  new optional `on_interim_text(text, round_num)` hook (threaded via
  `set_log_context()`'s thread-local, like `on_tool_usage`), (3) wired out
  through `ai_client.ask(on_interim_text=...)` (initial attempt and the D5
  short-429 retry) to `cli.py`'s always-on stderr trace line `» <text>`,
  which `server.js` already forwards to the web console, and (4) saved in the
  conversation as an `"interimText"` extra
  (`{"items": [{"round": N, "text": "..."}]}`), same pattern as `"thinking"`.
  **Gemini, OpenAI-compatible, Cohere and Ollama are untouched** and still
  discard interim text. This is narration-level surfacing (whole message,
  before tools run), **not** token streaming — that is §8.
- **Tests:** `tests/test_thinking_round_cap.py` (12 checks, isolates the cap
  from the `round_patch` floor by monkeypatching it) and
  `tests/test_interim_text.py` (20 checks, HTTP layer faked, plus one full
  `ai_client.ask()` integration test).
- **Regression suites re-run clean:** `test_discovery_round_budget` 41,
  `test_short_429_wait` 24, `test_forced_ending` 30, `test_key_health` 36,
  `test_ollama_openai_compat_thinking` 20, `test_prompt_cache` 44,
  `test_schemas_for_tools` 14, `test_code_agent_budget_and_outcomes` 51;
  `compileall` clean. `test_replay_fixtures.py`'s two cases skip in that
  sandbox (fixture files absent) — pre-existing.
- **Not verified:** end-to-end against a real Anthropic key (no network).
- **Still open in Part A:** §5 on the other four adapters, then §8. §3
  remains held off by the owner.

**Revision 2026-09-21c — Part 0 gaps closed and a codebase cross-check against `jarvis-main_43_`.**
Documentation only; no code changed. The plan's status claims were checked by
reading and *running* the tree (all 62 standalone `tests/test_*.py` scripts run
individually: 61 pass, 1 fails — the known `test_subagents.py` bug). Full
findings in the new §0.5. Headlines:

- **Part H was missing from Part 0's status lists** — added (Not started;
  confirmed: no `daemons/backlog/logsearch` `.js`/`.css` files exist, and none
  of `tool_schedule_task` / `tool_remind_me` / `tool_schedule_watch` notifies at
  creation).
- **Part E is not "nothing exists"**: a fragile `command_run` persistence for
  the Live Feed already ships (E.1's own table says so). Part 0 now says
  "baseline exists, redesign not started".
- **D.2 missed `digest.py`**: a notification digest (priority-based batching
  into one scheduled summary message, `jarvis digest-on/off/now/preview/status`)
  already exists. It is *not* the reply-summary D.2 asks for, but D.2 and Part 0
  must be reconciled with it.
- **§0.1b's suggested fix for the `_eligible_providers()` bug is unsafe.** It
  says to assign `providers = pinned` only when `pinned is not None`; that makes
  a malformed subagent pool fall through to the *main* provider list — the exact
  leak the boundary exists to prevent. Fail closed instead (§0.5 item 3).
- **§5's saved `interimText` extra is never rendered** by the web UI, and the
  live `»` line is truncated to 240 characters (§0.5 item 4).
- **F.17's replay fixtures are absent from this zip** (`tests/fixtures/` does
  not exist), so `test_replay_fixtures.py` reports 0 passed / 2 skipped here.

**Revision 2026-09-21d — G.1 delivered (a tool module supplies its own Test
Checklist entry).** `jarvis-test-checklist-supplied-entries.patch`, diffed
against `jarvis-main_44_` (apply with `cd jarvis-main && patch -p1 < …`; it
does not touch `ai_providers.py`, `ai_client.py`, `app.js`, `server.js` or
`index.html`, where the held-off/in-flight items — §5, F.1/F.8/F.11, Part C, §3,
Part E — would live; the other agent's tree was not visible, so check for overlap
in `tools.py` and `custom_tools_store.py` before merging). Part G contained
exactly one open item, G.1, so "the rest of the G family" is the plumbing G.1
needs: the loader, the API payload, the panel merge, the Custom Tools editor and
templates, and the docs. What now exists:

- A module can define `TEST_CHECKLIST` (tool name → entry, the shipped shape)
  and, for a brand-new `TOOL_GROUP`, `TEST_CHECKLIST_GROUP = {label, blurb}`.
  A user's own custom tool (`~/.jarvis/tools/`) finally has somewhere to put an
  entry; before this it showed **NO CHECKLIST** forever.
- **One definition of "well formed":** new `checklist_schema.py`, used by the
  loader, the Custom Tools editor **and** `tests/test_checklist_coverage.py`
  (which no longer carries its own copy of the rules). The plan's "same
  validation covers both without a second code path" is literally true.
- `GET /api/tools` stays a plain list; a tool that supplied an entry carries
  `checklist` (+ `checklist_group`). No new route, no new CLI command — so
  nothing to add to the three diverging `RESERVED_NAMES` sets (I-B2).
- `actions/_template.py` gets a documented section 8 (with a live, validated
  example), all four Custom Tools templates carry a `TEST_CHECKLIST`, the
  `~/.jarvis/tools/README.md` lists the fields, `test-checklist-data.js`'s
  FORMAT header now says there are two homes, and AGENTS.md's Test Checklist
  rule is reworded to match. Full detail in G.1 ("As built") below.

Verification: full suite on a fresh unzip with the patch applied — 62 scripts
pass, 1 fails (`test_subagents.py`, the pre-existing `_eligible_providers()`
`NoneType` bug, identical failure before and after). New
`tests/test_checklist_supplied.py` is 18 checks (including the JS merge under
node and an end-to-end run through a real `~/.jarvis/tools` file). **Not
verified:** the panel in a real browser (no browser in the sandbox).

**Revision 2026-09-21e — adds Part J.** This plan's actual last step, added
now so it isn't forgotten once everything above ships: a Test Checklist entry
for every one of the 16 tools this plan adds (Parts B/C/F.3), and a
concrete, testable item — an Ask prompt or a manual step, plus what a pass
looks like — for every non-tool feature this plan adds or changes, across
every Part, consolidated in one place. No code was changed; this only adds
the plan section and points at where each piece of coverage already lives
(§8.8, E.7, F.18, I.5) versus where it still needs to be written.

**Revision 2026-09-21f — adds G.2, and Part J.4.** Two additions: **G.2**
is a new requirement, not yet built — a module (custom tools included) can
already supply more than one *tool* checklist entry (nothing in G.1's schema
capped that; `browser_tools.py` already does it, one group + seven tool
entries), but that isn't said anywhere a non-implementing user would read,
and a module still can't supply more than one *group* entry — G.2 asks for
both to be documented, the second one built, in this section and in
`actions/_template.py`. **Part J.4** adds multi-step scenario coverage —
persistent browser login across separate `jarvis ask` invocations, all four
of F.8's failure shapes individually, the F.17 replay fixtures run end to
end, key-health cooldown across a process restart, and similar — for the
tools and fixes in this plan where a single Ask prompt doesn't exercise the
real risk. No code was changed by either addition.

**Revision 2026-09-22a — merges `jarvis-partC-D2.patch` and
`jarvis-finish-signal5andF.patch` into `jarvis-main_45_`.** Both were built
independently against the `jarvis-main_44_`/doc-16.1 baseline (in parallel
with G.1, and blind to it and to each other), so all three independently
call themselves "revision 2026-09-21d" — see §0.1f for the full reconciliation.
This delivers: Part C v2 (the warm browser daemon), D.2 (notification
summaries), §5 finished on the remaining four adapters, and F.1/F.8/F.11's
open remainder (the pending proposed action). One real conflict, hand-resolved
(`AGENTS.md`'s test-run list — see §0.1f); everything else applied cleanly.
Verified: `py_compile` clean; the three new test files pass in full (38 + 18
+ 121 = 177 checks); the full 66-script suite is 65/66, the one failure being
the pre-existing, unrelated `test_subagents.py` bug. A combined patch against
`jarvis-main_45_` is included alongside this doc. **Part E (console
persistence and filter) is explicitly not part of this merge — still not
finished, per the owner, as of this revision.**

**Revision 2026-09-22b — research-only additions from the supplied 2026-09-22
100k-token-burn log.** No codebase changes were made. This revision adds three
plan items: end-to-end **notification importance levels** (using the existing
`low` / `normal` / `high` model already defined in `digest.py`), **clipboard
notification flood protection**, and **100k+ input-token burn containment**
based on the new JSONL trace. Part J now includes concrete tests and Part K
adds the implementation/acceptance backlog. The supplied `AGENTS.md` was read
and its invariants are preserved here; the archive itself was inspected
read-only.

**Contents**

- [Part 0 — Status: what the patch delivered, what didn't get done, suggested order](#part-0--status)
- [Part A — Bugs, fixes and features from the log analysis (§1–§8, incl. real-time streaming)](#part-a--bugs-fixes-and-features-from-the-log-analysis)
- [Part B — Clipboard tool](#part-b--clipboard-tool-clipboard_toolspy)
- [Part C — Browser control via Playwright](#part-c--browser-control-via-playwright-browser_toolspy)
- [Part D — Remaining work the patch left open (subagent live view, notifications summary, scheduler reminder)](#part-d--remaining-work-the-patch-left-open)
- [Part E — Console output: conversation persistence (tried many times, never worked) and output filter](#part-e--console-output-conversation-persistence-and-output-filter)
- [Part F — Live failures of 2026-09-20: why Jarvis can't do basic tasks (shell tool, hidden files, no move tool, discarded tool calls, failover, routing)](#part-f--live-failures-of-2026-09-20-why-jarvis-cant-do-basic-tasks)
- [Part G — Test Checklist panel (web console)](#part-g--test-checklist-panel-web-console)
- [Part H — UI: Daemons/Backlog/Log search rework, a slighter Schedules pass, and a notification on job creation](#part-h--ui-daemonsbackloglog-search-rework-a-slighter-schedules-pass-and-a-notification-on-job-creation)
- [Part I — Chat UI: code blocks with one-click copy, the `/` command palette, and bugs found on the way](#part-i--chat-ui-code-blocks-with-one-click-copy-the--command-palette-and-bugs-found-on-the-way)
- [Part J — Final step: a checklist entry for every tool, and a test for every feature, this plan adds](#part-j--final-step-a-checklist-entry-for-every-tool-and-a-test-for-every-feature-this-plan-adds)
- [Part K — Current 2026-09-22 audit: what exists, what is missing, priorities and exact next steps](#part-k--current-2026-09-22-audit-what-exists-what-is-missing-priorities-and-exact-next-steps)

---

# Part 0 — Status

### 0.0 Authoritative current status — audited against `jarvis-main(47).zip` on 2026-09-22

> **Authority rule.** This section and Part K are the current source of truth.
> Sections 0.1 onward preserve the earlier revision history and intermediate
> states so that the reasoning behind the plan is not lost. Where they disagree
> with this section, this section wins.

### 0.0.1 Executive status

The current archive is **substantially ahead of the original plan snapshot**:
Parts B and C are implemented, most of Part A is implemented, the F-family is
mostly closed, G.1 is implemented, D.2/D.4 are implemented, and the current
repo also contains the Part E console store/persistence baseline. The largest
remaining product work is **Part I (code blocks + full `/` command palette),
Part H (UI rework), Part E's Ask-console replay/filter gaps, Part A §8
real-time token streaming, D.1 subagent live detail, and G.2 multi-group
checklist support**.

There are also **three current release/QA issues that are not safely represented
by the old status text**:

1. **Subagent key-isolation malformed-pool bug — OPEN / P0.** If
   `JARVIS_SUBAGENT_KEYS` is set but contains malformed JSON or a non-object,
   `providers_from_env()` returns `None`; `_eligible_providers()` then iterates
   that `None` and crashes. The code comments say this boundary must fail
   closed, but the provider parser still returns `None` for two malformed
   shapes. The fix is to return `[]` for malformed subagent pools, never the
   ambient/main provider list.
2. **Build/source-hash mismatch — OPEN / P0.** The checked-in
   `jarvis-cli/jarvis/build_info.py` says build 26, timestamp
   `2026-09-22 18:05:10`, source hash
   `62765d15d37183066ac4a2e35122111765d14541cfdd62a725665ffe2dee87b9`,
   while the repo's own `build_tools.hash_source.hash_source_tree()` computes
   `4fac34eb84638c653e3af2cde7cce8a9f786512118ad32d2ca94cc59c5b209d0` for the
   current source tree. A current executable therefore cannot be treated as a
   byte-for-byte build of this archive until the build metadata is regenerated
   and the executable is rebuilt/verified.
3. **F.17 replay fixtures are absent — OPEN / P1.** The current archive has no
   `tests/fixtures/` directory, while `test_replay_fixtures.py` expects the two
   real-log fixture pairs. The test consequently skips 2 cases. The earlier
   “fixtures built and run” claim belongs to an earlier archive and is not
   reproducible from `jarvis-main(47).zip`.

### 0.0.2 Current Part-by-Part matrix

| Part / item | Current status | Priority | What exists now | What is still missing / caveat |
|---|---|---:|---|---|
| **A — §1/§1b/§1c** | **DONE** | — | Request-shape failover, local-capacity guard, nullable/withheld-tool handling | Real provider matrix still benefits from live-provider QA |
| **A — §2 OCR diagnosis** | **DONE** | — | Prefix-based OCR diagnosis and drift-guarded tests | Real Windows Tesseract + long-running server restart not verified here |
| **A — §3 source filter** | **PARTIAL** | P2 | Debug source filter for builtin/auto/user tools | MCP tools are not merged into the Debug catalog, so “MCP” is not a source bucket yet |
| **A — §4 Ollama thinking** | **DONE** | — | OpenAI-compatible Ollama thinking path | Real Ollama server not verified here |
| **A — §5 interim text** | **DONE (all five adapters)** | — | Anthropic, Gemini, OpenAI-compatible, Cohere and Ollama surface interim text; saved `interimText` is rendered by current web UI | True token streaming is still §8, not §5 |
| **A — §6 thinking between tool calls** | **DONE** | — | `low=2`, `medium=4`, `high=uncapped` via shared reasoning helper | Overall `MAX_TOOL_ROUNDS=5` still limits the whole turn; low-level Anthropic/Gemini budget floor remains unchanged |
| **A — §7 auto-discovery fallback** | **DONE** | — | Keyword fallback for auto-discovered tools | — |
| **A — §8 real-time streaming** | **NOT STARTED** | P2 | No provider-wide token streaming path | Needs transport, provider adapters, AI client callback, web WS/SSE framing, UI render entry point and tests |
| **B — Clipboard** | **DONE** | — | Get/set/clear/wait, watch daemon, human config command, model set/get-pattern tools | Real OS backend verification is still outstanding |
| **C — Browser** | **DONE (v1+v2)** | — | Playwright tools + warm browser daemon + persistent profile + fallback | Real Chromium/login persistence and hard-kill cleanup are not verified |
| **D.1 — Subagent live view** | **PARTIAL** | P2 | Overlay, badge, cycling, plan/history/result, transcript link | Near-live incremental transcript/console/thinking and docked side-pane experience remain |
| **D.2 — Notification summaries** | **DONE** | — | Shared stdout stripping/summarization; web “Show more” | Digest is a separate feature, not a replacement for this |
| **D.3 — scheduled-reminder issue** | **NO ACTIONABLE OPEN BUG IDENTIFIED** | P3 | Scheduler already logs scheduled prompts and current prompt assembly has no matching stale block | Re-open only with a concrete current repro/provider requirement |
| **D.4 — nullable optional schema** | **DONE** | — | Current nullable/optional handling is tested | Historical wording should be treated as resolved |
| **E — console persistence/filter** | **PARTIAL** | P1 | Real append-only console store, replay API, live-run persistence, 3-group filter, search, clear | Ask console trace (`askTraceByConv`) is still memory-only and not replayed from persistent store; requested richer filter UI is absent; crash-recovery pointer/legacy cleanup remain |
| **F.1/F.8/F.11** | **DONE** | — | Forced ending, grace call, tool-less final request, pending action + “go ahead” | Real-model replay still depends on restored fixtures |
| **F.2 discovery budget** | **PARTIAL** | P1 | Separate catalog discovery budget exists (default 3) and tests pass | Project-content discovery (`search_files`/related content lookup) still consumes the normal work budget |
| **F.3/F.4/F.5/F.7/F.9/F.10/F.12** | **DONE** | — | Path ops, Windows shell args/diagnosis, dotfiles, failover transcript, key health, routing/paste/highlight, labels/confirm allow-list | Real Windows/live-model QA still needed |
| **F.6 last words** | **PARTIAL** | P2 | Round budget prompt and compact outcome log are done | A separate structured “model’s last words” field is not captured across adapters |
| **F.17 replay fixtures** | **MISSING FROM CURRENT ARCHIVE** | P1 | Replay harness exists | Restore the 2 fixture pairs and re-run both real scenarios end-to-end |
| **G.1 Test Checklist** | **DONE** | — | Module-supplied entries, common schema, editor integration, exact tool coverage | Human tightening / real-browser QA still needed |
| **G.2 multi-group supply** | **NOT STARTED** | P2 | Multiple tool entries per module already work | One module cannot currently supply multiple checklist groups |
| **H — Daemons/Backlog/Logs/Schedules UI** | **PARTIAL / REWORK OPEN** | P2 | Working baseline panels already exist; daemon restart counts exist in backend; step-editor drag/reorder exists, but not as the backlog feature | Requested Test-Checklist-quality rework is unfinished; log deep search still Enter-driven; backlog/daemon/schedule UX remains the main gap |
| **I — Code blocks + `/` palette** | **NOT STARTED / BUGS STILL PRESENT** | P1 | Current legacy markdown/math pipeline and legacy `/skillload` suggester exist | I-B1 and I-B2 remain; requested full code-block and command-palette implementation is absent |
| **J — final QA closure** | **NOT STARTED** | P1 | Checklist framework + acceptance sections exist | Human review, real UI/model runs, fixture restoration and cross-part acceptance still need closure |

### 0.0.3 Current verification result

The current tree was inspected directly, including the current source,
`REPO_MAP.md`, web UI, server routes, tool registry, checklist catalogue and
68 standalone `tests/test_*.py` scripts. A targeted current-tree test pass
covered the newly relevant reliability/browser/clipboard/console/interim-text/
router/path/tooling suites and those passed. The one reproducible current code
failure is `tests/test_subagents.py`'s malformed-pool case (`TypeError:
'NoneType' object is not iterable`). `tests/test_replay_fixtures.py` exits
cleanly but skips two cases because the fixture directory is absent.

Some browser/desktop tests cannot be treated as product failures in this
Linux sandbox because the real UI backends are unavailable; the fake-browser
and Xvfb-assisted tests were used where possible. A network-oriented MCP test
was not counted as a failure when it timed out under the sandbox. This is an
**environment limitation, not an assertion that the feature is correct**.

### 0.0.4 Priority legend

- **P0 — release/security gate:** fix before treating the archive as a trusted
  distributable or relying on the boundary in production.
- **P1 — high-impact correctness/regression/product blocker:** close before a
  serious release cut; these either reproduce today or block meaningful QA.
- **P2 — major planned functionality / integration / UX:** substantial work,
  but not the immediate integrity blockers.
- **P3 — polish, maintenance, documentation, or work that needs a concrete
  repro/owner decision first.

### 0.1 Already delivered by the patch (merged into repo 22)

| # | Item | Notes |
|---|---|---|
| 1 | **Subagents button** in the topbar, right next to *Ask Jarvis*, with a live count badge | `index.html`, `app.js` |
| 2 | **Subagents panel**: Active / Finished-recently lists, prev/next cycling, per-subagent detail (status, steps used / max, plan with done/failed/skipped steps, step history, result), Refresh, Cancel | polls every 4 s while open |
| 3 | **Real transcript per subagent**: `subagents.spawn()` now mints a real conversation (`conv_id`) for every subagent, so every step of a task lands in one transcript instead of orphaned anonymous conversations. "View full transcript" loads it into the Ask panel (real tool calls, console output, thinking) | only subagents spawned *after* this change have one; older tasks have no `conv_id` and the button is disabled |
| 4 | **REST + CLI plumbing**: `GET /api/subagents`, `GET /api/subagents/:id`, `POST /api/subagents/:id/cancel`; `jarvis subagents` now returns structured per-task fields; `subagent-status` returns plan/history/conv_id/steps | `server.js`, `cli.py`, `subagent_tools.py` |
| 5 | **Notifications no longer contain raw `JARVIS_USAGE {...}` / `JARVIS_CONFIRM_REQUEST {...}` JSON** for scheduled asks and scheduled commands; the raw capture is still written to the ask log (Log search greps it) | `scheduler.py` (`_strip_protocol_lines`) |
| 6 | **Collapsible "Thinking (N rounds, level)" block** rendered from the saved `thinking` extra (previously that data reached the browser and was silently dropped), plus its CSS | after-the-fact only — see 0.2 |

**Deliberately NOT taken from the patch:** its versions of the Daemons /
Backlog / Log search panels. In repo 19 those were plain `.menu-overlay`
panels; repo 22 already moved them onto the bigger `.debug-overlay` chrome
with card styling. The patch would have reverted them to the older look, so
22's reworked versions were kept. Same for four `style.css` position tweaks
(`.bottom-status-bar`, `.notifications-fab`) where 22 already had newer values.

### 0.1b Delivered since this document was written (4 patches, all from §0.4 step 1's "quick, independent fixes")

| # | Item | Patch | Notes |
|---|---|---|---|
| 1 | §1c — `tool_choice`/withheld-tools 400s and the `parent_id: null` schema rejection | `jarvis-request-shape-failover.patch` | Touches `ai_client.py`, `ai_providers.py`, `tools.py` + 2 new test files (24+43 checks) |
| 2 | §1b — capacity auto-escalation must not apply to local models | `jarvis-capacity-local-guard.patch` | Touches `mode_tools.py` + 1 new test file (20 checks) |
| 3 | §4 — Ollama thinking mode not triggered for the second local provider (`ollama1`) | `jarvis-ollama-openai-compat-thinking.patch` | See "What's actually implemented" note in §4 below. **Unverified against a real Ollama server** (no network in the sandbox): the request shape is right, whether Ollama honors it is untested |
| 4 | §7 — auto-discovered tools with no `TOOL_KEYWORDS` had zero router coverage | `jarvis-auto-discovered-tool-keyword-fallback.patch` | See "What's actually implemented" note in §7 below. The fallback keywords are a floor, not a substitute for real `TOOL_KEYWORDS` |

Two corrections to §1c/§1b's analysis as originally written in Part A,
found while implementing them (the text below has since been left as
originally written for the historical record; treat these as overriding
it):

- **§1c:** nothing in the code ever set `tool_choice`. The 400 happens when
  a round withholds tools and the model calls one anyway — a one-shot retry
  for that already existed (`_looks_like_omitted_tools_confused_the_model`).
  The real gap the patch closed was `ask()` rotating to the next API key
  after *every* failure, including rejections no key can fix.
- **§1b:** no code anywhere raises capacity on failure or retry. The only
  writers of `prompt_mode` are a person (web UI / `jarvis mode-set`) and the
  model-callable `set_capacity_mode` tool — so "the model did it" in the
  original log is a hypothesis, not a confirmed mechanism (the log that
  prompted this section wasn't available to check it against). The guard
  landed in `set_capacity_mode` itself regardless, since refusing to
  escalate while a local model answers is correct either way.

**A pre-existing, unrelated bug found and left for a decision, not fixed
by any of the four patches above:** `test_subagents.py`'s
`test_malformed_pool_yields_nothing_not_main_key` fails on both the
original baseline and every tree with these patches applied — same
failure, unrelated to any of them. `_eligible_providers()` in
`ai_client.py` calls `subagents.providers_from_env(providers)` and does
`providers = pinned` unconditionally; `providers_from_env()` returns
`None` both for "not a subagent, keep the original list" and for "a
subagent pool that's set but unparseable JSON" — `_eligible_providers()`
can't tell those apart, so unparseable JSON overwrites `providers` with
`None` and the next line's `for p in providers` throws
`TypeError: 'NoneType' object is not iterable`. It reproduces for anyone,
any environment — one of the existing test's cases (a malformed-JSON
pool) hits it directly. It's a one-line fix — **but the fix described in this sentence is unsafe; see §0.5 item 3 (2026-09-21c), which supersedes it** (`_eligible_providers` should
only assign `providers = pinned` when `pinned is not None`) but is being
left for a deliberate decision rather than folded into an unrelated patch:
fix now as its own tiny patch, or bundle it alongside §4/§7's neighbors
(§2/§3) next.

### 0.1c Delivered in the 2026-09-20g–m batch (6 patches, merged into `jarvis-main_31_`)

| # | Item | Patch | Notes |
|---|---|---|---|
| 1 | Part B — clipboard tools | `jarvis-clipboard-tool.patch` | `clipboard_tools.py`, `"clipboard"` group; 15 checks |
| 2 | Part B — continuous watch | `jarvis-clipboard-watch.patch` | Built-in daemon, not a model-callable start/stop tool; 14 checks. D7 done 2026-09-21 |
| 3 | Part C — browser control | `jarvis-browser-control.patch` | `browser_tools.py`, `browser-setup`, click/fill confirm-gated; 28 checks |
| 4 | F.3 — path tools | `jarvis-path-tools.patch` | `actions/path_tools.py`; `tool_safety.py` untouched; 106 checks |
| 5 | F.1/F.8/F.11 — forced ending | `jarvis-forced-ending.patch` | `ai_client.py` + `ai_providers.py`; grace call on by default; 30 checks |
| 6 | Test Checklist panel | `jarvis-test-checklist.patch` | Front end + AGENTS.md rule + `test_checklist_coverage.py` |

Full detail and the merge notes are in revisions 2026-09-20g–m at the top.

### 0.1d Delivered in revision 2026-09-21b

| # | Item | Patch | Notes |
|---|---|---|---|
| 1 | §6 — thinking between tool calls | `phase-a-partA-56.patch` | `reasoning.max_thinking_rounds(level)`; `low` 2 / `medium` 4 / `high` uncapped; all adapters; 12 checks |
| 2 | §5 — interim text with tool calls (**Anthropic only**) | `phase-a-partA-56.patch` | `get_interim_text()`, `on_interim_text` hook, `interimText` extra, CLI `»` trace line; 20 checks |

Touches `ai_providers.py`, `ai_client.py`, `reasoning.py`, `cli.py` + 2 new test files. Apply with `cd jarvis-main && patch -p1 < phase-a-partA-56.patch`. Full detail in revision 2026-09-21b and Part A §5/§6.

### 0.1e Delivered in revision 2026-09-21d

| # | Item | Patch | Notes |
|---|---|---|---|
| 1 | G.1 — a tool module supplies its own Test Checklist entry | `jarvis-test-checklist-supplied-entries.patch` | New `checklist_schema.py`; `tool_loader.py`, `tools.py` (payload), `custom_tools_store.py` (check/write report + templates), `actions/_template.py` §8, `test-checklist.js` (merge), `custom-tools.js` (notes), `test-checklist-data.js` (`custom` group, header), AGENTS.md / REPO_MAP.md / web README; 18 new checks + `test_checklist_coverage.py` reworked |

Apply with `cd jarvis-main && patch -p1 < jarvis-test-checklist-supplied-entries.patch`. Full detail in G.1 ("As built") below.

### 0.1f Delivered in revision 2026-09-22a — two patches built in parallel, merged together

**Three patches — G.1 (above), this section's two — were each built independently
against the same `jarvis-main_44_`/doc-16.1 baseline, blind to one another, and
each one's own materials independently call itself "revision 2026-09-21d."
That collision is expected, not an error — see G.1's own As-built note about
"the other agent's tree was not visible." This section is where the other two
get reconciled against `jarvis-main_45_` (which already carries G.1) and
against each other; 2026-09-22a is the merge's own revision, not a relabeling
of either patch's own "21d."**

| # | Item | Patch | Notes |
|---|---|---|---|
| 1 | Part C v2 — warm browser daemon | `jarvis-partC-D2.patch` | New `browser_daemon.py` (BUILTIN `jarvis browser-daemon`); `browser_tools.py` gets the `browser_warm_daemon` flag (off by default); `daemons.py`, `cli.py`; 18 checks |
| 2 | D.2 — notification summaries | `jarvis-partC-D2.patch` | New `ask_output.py` (`strip_protocol_lines`, `summarize`); `notifier.py`, `scheduler.py` (re-exports the shared helper), `task_runner.py` (the second leak site the §0.5 audit had flagged); `app.js`/`style.css`; 38 checks |
| 3 | §5 — the remaining four adapters, and the finish-signal loop itself | `jarvis-finish-signal.patch` | `ai_providers.py` gains `finish_signal()`; `_surface_interim_text()` now runs on Gemini/OpenAI-compatible/Cohere/Ollama too; truncated tool calls never run (`KIND_CUTOFF`); a cut-off answer is reported, not presented as finished; 121 checks |
| 4 | F.1/F.8/F.11's open remainder — the pending proposed action | `jarvis-finish-signal.patch` | `ai_client.py`/`turn_trace.py` gain `AskResult.ending`/`TurnTrace.ending` (forced endings reported as forced, not "every provider failed"); `_pending_action_extra`/`_load_pending_action`, 30-min TTL, owner surfaces only, still through `tool_safety.py`'s confirm gate |

**Merging the two against `jarvis-main_45_` and against each other.** Applied
`jarvis-partC-D2.patch` first, then `jarvis-finish-signal.patch`, both with
plain `patch -p1`, on top of a fresh `jarvis-main_45_` extraction (i.e. on top
of G.1, already merged):

- **Real conflict: `AGENTS.md`.** Both `jarvis-finish-signal.patch` and G.1
  (already in `jarvis-main_45_`) insert a line into the same spot in the
  `## Testing` run-list — G.1 added `test_checklist_supplied.py` right where
  `jarvis-finish-signal.patch`'s hunk expected to find a blank line next.
  Hand-resolved by keeping both lines, in the order each patch added them,
  plus two lines `jarvis-partC-D2.patch` should have added but didn't
  (`test_ask_output.py`, `test_browser_daemon.py` — that patch's list of
  changed files never includes `AGENTS.md`, so its two new test files were
  never listed there; added here rather than left silently missing).
- **Checked, not a conflict: `REPO_MAP.md`, `cli.py`, `web/public/app.js`.**
  All three are touched by more than one of the three patches (G.1 touches
  the first; `jarvis-partC-D2.patch` and `jarvis-finish-signal.patch` both
  touch all three) but land in different regions of each file — `patch -p1`
  applied every hunk in all three with zero rejects, and the resulting files
  were read to confirm the coexisting changes make sense together (e.g.
  `app.js`'s notification "Show more" toggle and its `renderThreadExtra`
  `"interimText"` case are unrelated call sites).
- **No overlap at all:** the rest of `jarvis-partC-D2.patch`'s files
  (`ask_output.py`, `browser_daemon.py`, `browser_tools.py`, `daemons.py`,
  `notifier.py`, `scheduler.py`, `task_runner.py`, `style.css`, its two new
  test files) and the rest of `jarvis-finish-signal.patch`'s
  (`ai_client.py`, `ai_providers.py`, `turn_trace.py`, its new test file)
  don't intersect each other or G.1 at all.

**Verification.** `python3 -m py_compile` clean on every changed/new `.py`
file. All three new test files pass in full against the merged tree
(`test_ask_output.py` 38/38, `test_browser_daemon.py` 18/18,
`test_finish_signal.py` 121/121 — 177 checks). Full `tests/test_*.py` suite,
each run directly per `AGENTS.md`: 66 scripts, 65 pass, 1 fails
(`test_subagents.py`'s pre-existing `_eligible_providers()` `TypeError`,
identical failure before and after — see §0.1b/§0.5 item 3, still not this
merge's to fix). A combined patch representing both `jarvis-partC-D2.patch`
and `jarvis-finish-signal.patch` plus the `AGENTS.md` hand-resolution,
diffed against `jarvis-main_45_`, applies cleanly with `patch -p1` to a
fresh `jarvis-main_45_` extraction and reproduces the merged tree
byte-for-byte — sent alongside this doc.

**Explicitly still open, per the owner (2026-09-22):** the Part E patches
(console output persistence and filter) are **not finished yet** and are not
part of this merge — Part E's status below is unchanged from doc rev.
2026-09-21f. §8 (real-time streaming) and Part D.1's true live subagent view
still depend on §5, which is now finished on every adapter, but neither was
started here.

### 0.2 Partly done

| Item | What exists | What's missing |
|---|---|---|
| Watch subagents "right next to Jarvis" with their console output and reasoning | A button + panel with cycling, plan, step history and a transcript link | It's an **overlay panel** that polls every 4 s, not a docked side view; reasoning/console output only appears via the finished transcript, **not live**. Live view depends on §8 streaming — design in Part D.1 |
| "Thinking" like Claude/Cursor (word by word) | Post-hoc collapsible block; a show/hide toggle already exists (`Show reasoning trace`) | Nothing streams — the whole of **§8** is still to do (§5, its prerequisite, is now finished on all five adapters — see §0.1f) |

### 0.3 Not started

- Most of Part A: §3 (Debug source filter — **held off by the owner for now**), §8 (real-time streaming on all five adapters — its one prerequisite, §5, is now finished on every adapter, see §0.1f)
- Items from your own scheduler reminder (Part D.3)
- **Console output conversation persistence** — attempted many times before without success; root causes found in the code are in Part E.3. **Still not finished as of 2026-09-22** — see §0.1f; not part of this merge.
- **Console output filter** — choose what you want to see in the console output (Part E.5). Same as above: not finished.
- The pre-existing `_eligible_providers()`/`providers_from_env()` `None`-vs-`None` bug noted in §0.1b — a decision, not a start-from-scratch item
- **Part F — what's left.** Every finding has shipped its core, and as of 2026-09-22 **F.1/F.8/F.11 are fully done** (see §0.1f) — D5/D6/D7 and the F.17 fixtures were already done (2026-09-21). What remains is F.2's discovery-budget gap for project-content tools and F.6's "last words" (both need the shared adapter loop, which §5 finishing on all five adapters now provides — not yet applied to F.6 specifically).
- **Part I** — code blocks with one-click copy, the rebuilt `/` command palette, and the twelve bugs found while researching them (I-B1–I-B12). Owner decisions are in I.4; the two small reproduced fixes (I-B1, I-B2) can go first.
- **Part H** — see 0.0 above (H.1 dashboards-and-files rework, H.2 Schedules pass, H.3 creation-time notification).

**Done, no longer in this list:** G.1 — see §0.1e; §1c, §1b, §4, §7 — see §0.1b; §6 and §5-on-Anthropic — see §0.1d; Part B, Part C, F.3 and most of F.1/F.8/F.11 — see §0.1c; §5 (remaining four adapters), F.1/F.8/F.11 (fully), Part C v2, D.2 — see §0.1f.

### 0.4 Suggested order of work

1. **Quick, independent fixes** (small, each stands alone): ~~§1c~~, ~~§1b~~, ~~§4~~, ~~§7~~ — done, see §0.1b. §2's diagnosis bug is now fixed too (rev. p; what's left is the PATH step in §2). Left in this batch: §3 (Debug source filter — held off by the owner for now).
1b. **Basic-task reliability (Part F, tiers 0a–0c)** — *before* console
   persistence and streaming, because it is why simple requests fail today.
   Tier 0a is small and independent (F.4 shell, F.5 hidden files, F.2 unknown-tool
   hint, F.6 code_agent prompt/result, F.10 router, F.3 move tool as its own
   reviewed PR). Tier 0b touches the core loop and belongs **with step 3**
   below (F.1/F.8 forced ending, F.7 failover). Tier 0c: F.9 key health, F.11
   degraded replies, F.12 polish. Build the replay fixtures (F.17) first.
1c. **Part I quick fixes** — I-B1 (`extractMath` must skip code; `jarvis-math-extract-skips-code.patch`) and I-B2 (one reserved-names source; `jarvis-reserved-names-single-source.patch`). Both are small, independent and were reproduced by running the code; the only owner decision is D-I9.
2. **Console persistence — reproduce first** (Part E.6 step 0): it has failed many times, so write the failing reproductions before anything else; its cheap fixes (E.3 #5) can land immediately. **Still not finished as of 2026-09-22** — see §0.1f.
3. ~~**Core loop change**: §5 (finish-signal-driven turns, never discard interim text) and §6 (thinking policy)~~ — **done, all adapters**, rev. 2026-09-22a (§0.1f); §6 was already done on all adapters. §8's event vocabulary depends on this and can now start.
4. **Streaming** (§8): shared pieces first (`_post_stream`, event vocabulary, sink, `JARVIS_STREAM` marker, segmented UI), then adapters Ollama → openai_compatible → Anthropic → Gemini → Cohere. Not done until all five stream.
5. **Console store, ask-path wiring and filter** (Part E.4–E.5) — build together with §5/§8 so saved turns share one turn identity, not after them.
6. **Subagent live view** (Part D.1) on top of streaming.
7. ~~**Clipboard** (Part B)~~ — **done**, see §0.1c. (Was: small and independent; can be done in parallel with any of the above. Its optional watch feature needs `daemons.py`.)
8. ~~**Browser control** (Part C)~~ — **done (v1 + v2)**, see §0.1c/§0.1f. Reuses the confirm-gate (`tool_safety.py`); the v2 warm daemon (`browser_daemon.py`) shipped rev. 2026-09-22a, off by default behind `browser_warm_daemon`.
9. ~~**Part D.2**~~ — **done**, see §0.1f. **Part D.3** (scheduler-reminder items) whenever convenient.
10. **Part I** (code blocks with one-click copy, the `/` command palette) — front end only apart from one small read route. Land its render-pipeline consolidation (I.3 step 3) before §8's UI work so streaming has a single render entry point.
11. **Part H** — H.3 is small and independent (a `notifier.notify` call after `scheduler.create()` in three tools; mind the `tool_notify_me` no-`when` branch). H.1/H.2 are UI work; `ui-kit.js`'s popup layer (`JarvisUI.toast/dialog/confirm/choose`) already exists and is worth reusing.
12. **Part J — last, always.** Once everything above has actually shipped: tighten every draft tool checklist entry, write the still-missing feature checklist rows, and confirm nothing this plan touched is left at **NO CHECKLIST** or without a recorded Ask prompt. Don't start this early — a checklist entry written against a feature that's still in flux gets rewritten anyway.

**Cross-cutting rules for the new tool modules (Parts B and C):**
- When wiring `clipboard_*` / `browser_*` into `tools.py`, also give them a group and keywords in the router (`tool_registry.py` / `TOOL_KEYWORDS`) — otherwise they hit the same "never shows `routed:`" gap described in §7.
- Once §3 exists, the new tools should appear under the right source bucket in the Debug menu.
- New tools must return only schema shapes that tolerate `null` for optional string fields (§1c) — model-sent explicit `null` is normal and Groq's server-side validator rejects it.
- Destructive/side-effecting tools go through `tool_safety.py`'s confirm-gate, never one-off logic inside the tool module.

### 0.5 Codebase cross-check (2026-09-21c, against `jarvis-main_43_`)

Method: unzipped the tree, read the code each status claim depends on, and ran
every `tests/test_*.py` script on its own (they are standalone, not pytest).

**Verified as the plan says**
- §5 and §6 are in the tree (`reasoning._LEVELS[..]["max_thinking_rounds"]`,
  `ai_providers.get_interim_text`, `on_interim_text` through `ai_client.ask` and
  `cli.py`). `test_thinking_round_cap.py` 12/12, `test_interim_text.py` 20/20.
- §5 exists **only** inside `call_anthropic`; no other adapter references it.
- §3 is absent (no Debug source filter in `index.html` / `app.js`).
- §8 is absent: no `_post_stream`, no `JARVIS_STREAM`, Ollama sends
  `"stream": False`.
- Part H, Part I (no code-block copy buttons, no `/` palette beyond the
  `/skillload` autocomplete), and Part E's redesign are not built.
- I-B2 still reproduces: three diverging `RESERVED_NAMES` sets
  (`cli.py`, `commands_config.py`, `web/server.js`); `commands_config.py`'s is
  the smallest and lacks `think`.
- `MAX_TOOL_ROUNDS = 5` (AGENTS.md invariant), so §6's "uncapped" `high` is
  bounded by it: round 0 plus at most five follow-ups. `medium`'s cap of 4 and
  `high` therefore differ by at most two rounds in practice.
- Suite result: 61 of 62 scripts pass; the one failure is `test_subagents.py`
  (item 3 below).

**Found: the plan is wrong, stale, or silent**
1. **Part E baseline.** Part 0 called Part E "not started". The Live Feed's
   `command_run` persistence (`cli.py` `logs-append-run`, `server.js` `runOnExit`)
   already ships and is what E.3 #5 critiques. Accurate status: *baseline
   exists and is fragile; the redesign and filter are not started.*
2. **`digest.py` is unmentioned in D.2 and Part 0.** It implements notification
   priorities (`high` / `normal` / `low`) and a scheduled flush that sends one
   grouped summary (`jarvis digest-on`, `-off`, `-now`, `-preview`, `-status`).
   That covers *batching routine notifications*, not what D.2 asks (a short
   summary of a reply body instead of raw output). Decide whether D.2's summary
   should reuse the digest's grouping, and whether Part 0's "no actual summary
   yet" wording should change.
3. **`_eligible_providers()` bug — corrected fix.** Reproduced again:
   `test_malformed_pool_yields_nothing_not_main_key` dies with
   `TypeError: 'NoneType' object is not iterable`. `ai_client.py` already
   carries a comment saying this boundary must "fail closed", but
   `subagents.providers_from_env()` returns `None` for unparseable JSON or a
   non-dict pool, and `_eligible_providers` (which has already established the
   process *is* a subagent) assigns that `None` to `providers`. §0.1b's
   suggested fix — assign only when `pinned is not None` — would make a
   malformed pool fall through to the **main** provider list. **Do this
   instead:** make both malformed-pool returns in `providers_from_env` return
   `[]` (matching its own docstring's "a malformed pool means no usable
   providers"). Tried on a scratch copy: `test_subagents.py` passes in full,
   and `test_workspace`, `test_key_health`, `test_short_429_wait` still pass.
   Also note the existing test skips its `{not json` assertion with the
   comment "indistinguishable from not a subagent", which is only true of the
   pre-`is_subagent` code — it can be tightened to expect `[]` afterwards.
4. **§5 gaps the first write-up did not list.** (a) `interimText` is saved to
   the conversation but `renderThreadExtra` in `app.js` has no `interimText`
   case, so it is never shown after a reload — only the live stderr line shows
   it. (b) That live line is truncated to 240 characters in `cli.py`. (c) It is
   not in `_extras_recap_fragment`, so a later turn's recap will not mention
   it (probably fine, but it is a decision).
5. **F.17 fixtures are not in this zip.** `tests/fixtures/` does not exist, so
   `test_replay_fixtures.py` prints `0 passed, 0 failed, 2 skipped`. Revision
   2026-09-21's "Case 1 passes" cannot be reproduced from this archive; the
   fixture pairs (`cb140e091ddc66e8`, `81b52bb561796954`) need to be re-added.
6. **H.1's open question is answered.** `daemons.py` already tracks windowed
   restarts and its status output includes a `restarts` count, so H.1's
   crash-loop indicator likely needs no new server data; confirm the web route
   passes it through.
7. **Web files the plan barely covers:** `ui-kit.js` / `ui-kit.css` (the generic
   toast / bubble / dialog / confirm / choose layer, reachable from Python via
   `ui_bridge.py`) and `custom-tools.js` (the Custom Tools panel). They matter
   for Parts H and I (reuse rather than build new popups) and for the
   Test Checklist rule in AGENTS.md if custom tools ever join the catalogue.
   *(Resolved by G.1, rev. 2026-09-21d: custom tools join through their own
   `TEST_CHECKLIST`, and `custom-tools.js` now reports what was accepted.)*
8. **Unlisted modules.** Roughly fifty modules in `jarvis-cli/jarvis/` are not
   named anywhere in the plan (memory, skills, Spotify, Playnite, yt-dlp,
   Everything search, history summarizer, and so on). They are pre-existing
   features outside this plan's scope, not missed work; `REPO_MAP.md` is the
   map for them.

**No `TODO`/`FIXME` markers** exist in the Python or web code, so there is no
hidden backlog beyond what the plan lists.

---

# Part A — Bugs, fixes and features from the log analysis

> **CURRENT AUDIT — 2026-09-22:** Part A is mostly delivered. §§1/1b/1c/2/4/5/6/7 are current; §3 is partial because MCP tools are not exposed in the Debug tool-source catalogue; §8 (real-time token streaming) is still open. **§1g is a new open research/implementation item from the supplied 2026-09-22 JSONL trace: 123,368 reported input tokens across 70 usage records, with the same user request reproduced 572 times inside request payload history.** The old revision notes that say §5 only worked on Anthropic are historical; the current tree surfaces interim text on all five adapters.



> Source notes: grounded in `jarvis-cli/jarvis/*.py`, `web/*` and a ~25-minute trace of one real turn (`205b1493df955d1c.jsonl`). Line numbers were accurate for the repo copy it was written against — re-check if files moved. §5, §6 and §8 were re-checked against `jarvis-main`.

### Contents

1. [Log analysis — why a "quick" question burned 25 minutes and 14 keys](#1-log-analysis)
2. [Bug: Tesseract "on PATH" but OCR still fails](#2-bug-tesseract-on-path-but-ocr-still-fails)
3. [Feature: filter tool source in the Debug menu](#3-feature-filter-tool-source-in-the-debug-menu)
4. [Feature: Ollama thinking mode](#4-feature-ollama-thinking-mode)
5. [Feature: model can still call tools after talking to the user (the finish signal decides)](#5-feature-model-can-still-call-tools-after-talking-to-the-user)
6. [Feature: thinking between tool calls](#6-feature-thinking-between-tool-calls)
7. [Bug: auto-discovered tools don't show `routed: x`](#7-bug-auto-discovered-tools-dont-show-routed-x)
8. [Feature: real-time streaming of text AND thinking, on every provider](#8-feature-real-time-streaming-of-text-and-thinking-on-every-provider)

---

### 1. Log analysis

The log is one turn: *"could you research this codebase... i just wanna know
what does cli.py do... dont research it thoroughly."* A one-file lookup. It ran
from `23:25:47` to `23:50:36` — **almost 25 minutes** — and touched **14
different API keys** (10 Gemini, 4 Groq) before falling back to two local
Ollama models, neither of which ever completed the task; the log ends with the
user typing manual nudges, not with an answer. This is the clearest evidence
for "eating tokens for nothing... and then it's just dumb... times out," and
it's actually several separate, independently-fixable problems stacked on top
of each other.

#### 1a. The key-cascade itself

`provider` cycles: `gemini (key 1/10)` → `key 2` → `key 3` → back to `key 1`
→ `key 2`...`key 10` → `groq (key 1/4)`...`key 4` → `ollama` → `ollama1`.
Responses show why:

- Gemini key 1 hits `429` (quota exceeded) after only 2–3 successful calls,
  repeatedly. Whatever tier these keys are on, quota is exhausted almost
  immediately — a few hundred tokens in.
- Several `503 UNAVAILABLE` ("model experiencing high demand") on keys 2 and
  4, which are transient/retriable, not quota exhaustion — but they consume a
  key-rotation slot exactly like a 429 does.
- After all 10 Gemini keys are burned, it moves to Groq, then Ollama. Total:
  14 keys tried in one turn for one file lookup.

This part is arguably "working as designed" (failover is the point), but 10+4
keys for a single simple request suggests either the quota tier is too low for
real use, or the retry loop is cycling through keys faster than it should
(see 1c — some of these failures didn't need a *different key* at all, they
needed the *request itself* fixed).

#### 1b. Capacity auto-escalates mid-turn — and stacks with local inference

The `info` log lines show `capacity_mode` climbing over the course of this
single turn: `compact` (100%) for the entire Gemini/Groq phase, then once it's
on Ollama: `compact` → `precise` (150%) → `full` (400%). Something is treating
repeated failure/retry as a signal to *increase* the prompt/response budget.
That's a defensible idea against a cloud model (more room might help it
succeed) — it's actively harmful against a local model, where a bigger budget
directly means a much longer wall-clock generation. Two of the largest gaps in
the whole log (`23:36:14 → 23:40:18`, over 4 minutes; `23:42:35 → 23:45:54`,
over 3 minutes) both happen after this escalation, on `qwen2.5:14b-instruct`.

Worth checking: where capacity mode gets bumped on failure/retry (likely
something in `ai_client.py`'s retry path or `mode_tools.py`), and whether it
should ever auto-escalate at all when the active provider is local — or at
minimum should reset per-provider rather than carrying an escalated capacity
from the Gemini/Groq attempts into the Ollama fallback.

`keep_alive` itself looks fine — `prompt_cache.py`'s `DEFAULT_OLLAMA_KEEP_ALIVE
= "30m"` (line 179) is well above every gap observed here, so the model
almost certainly wasn't cold-reloading between calls. The multi-minute gaps
are most likely just genuine local-inference time for a 14B model at an
escalated capacity — not a bug in the keep-alive logic, but a bad interaction
between the escalation (1b) and local hardware.

#### 1c. Two Groq errors that key-rotation can never fix

Three separate `400`s across three different Groq keys, all the same error:

> `"Tool choice is none, but model called a tool"`

This is a **request-shape bug, not a quota/key problem** — the request set
`tool_choice: "none"` (probably meant to force a final text answer after
exhausting tool rounds) but still gave the model tools it then tried to call,
and Groq's server rejects that combination outright. Retrying it on a
different key reproduces the exact same 400 every time, which is exactly what
the log shows — three keys burned on a bug that would recur on a fourth,
fifth, or fiftieth key too. Worth checking wherever `tool_choice` gets forced
to `"none"` (likely near `MAX_TOOL_ROUNDS` in the round-budget logic in
`ai_providers.py`) — if tools are still being attached to the payload in that
same request, that's the bug; if tools are correctly omitted, then something
else is re-adding `tools` after `tool_choice: "none"` is set.

The fourth Groq key hit a *different* 400:

> `"Tool call validation failed: parameters for tool spawn_subagent did not
> match schema: errors: [/parent_id: expected string, but got null]"`

`subagent_tools.py` already handles this gracefully on the Python side —
`parent_id = args.get("parent_id") or os.environ.get("JARVIS_TASK_ID") or
None` (line 59) treats a missing/null `parent_id` as "use the current task or
none." The problem is purely in the JSON schema: `"parent_id": {"type":
"string", ...}` (line 225) declares it as strictly a string, so when the
model — reasonably, from its own perspective — sent `"parent_id": null`
instead of omitting the key, Groq's *server-side* schema validator rejected
the whole call before Jarvis's code ever saw it. **Possible fix:** widen the
schema to tolerate null (`"type": ["string", "null"]`) for `parent_id`, and
audit other optional-string tool parameters for the same gap — anywhere the
Python side already treats `None`/missing as equivalent, the schema should
say so too, since a model choosing to send an explicit `null` for an "optional,
defaults to X" field is a completely normal thing for a model to do.

Neither of these two errors is retriable by rotating keys, and both were
retried by rotating keys anyway — that's 3–4 wasted round trips right there.

#### 1d. On Ollama, the model narrated tool use instead of calling tools

This is the single biggest contributor to the 25 minutes. Once the log
reaches `ollama` (`qwen2.5:14b-instruct`) and later `ollama1`
(`deepseek-r1:14b-qwen-distill-q4_K_M`), the responses are things like:

> *"I will now search for `cli.py` in `D:\...\cli.py`. Let's proceed with
> that."*

with `finish_reason: "stop"` and **no `tool_calls` array at all** — plain
prose describing an intention, not an actual function call. The very last
request in the log has 22 messages in its history, and *every single one* of
the 10 injected "user" turns is the human manually pushing the model to
actually call something:

> `"yes go ahead you can do tool calls"` · `"use the code agent tool to
> inspect cli.py do it please now"` · `"you can search for the file using the
> file search command"` · `"oops my bad finish plz"`

This isn't a harness bug exactly — it's a local-model reliability problem
(smaller/quantized models are known to be much less consistent than
Gemini/Claude/GPT at emitting well-formed `tool_calls` instead of just talking
about the tool) — but the harness currently has **no way to detect or recover
from it**. It just returns the prose as a normal final answer, over and over,
leaving the user to notice and manually re-prompt each time. Possible fixes,
roughly in order of effort:

- **Detect the pattern server-side**: if a response's text strongly resembles
  a description of tool use (e.g. matches tool names verbatim, or phrases
  like "I will now use X") but `tool_calls` is empty, treat it as a
  non-answer and auto-retry once with a short forced nudge — automating
  exactly what the user in this log did by hand.
- **Force tool use more aggressively for local models**: Ollama's
  OpenAI-compatible endpoint accepts `tool_choice: "required"` on models that
  support it — worth trying when a round should clearly be calling a tool
  (e.g. router matched a specific group) rather than relying on `"auto"`.
  This may not be a real fix if the model genuinely can't produce valid
  tool-call JSON reliably.
- **Surface the failure honestly instead of finishing "normally"**: at minimum,
  a response with no tool_calls whose text is essentially "I'm about to do X"
  could be flagged in the UI/trace as low-confidence, rather than presented
  exactly like a real completed answer.
- Two hallucinated tool names also show up here (`code_inspect`,
  `code_snippet_analyze` — neither exists; `get_tool_schema` correctly
  returned `did_you_mean: ["code_agent"]` both times) before the model
  finally called `search_tools`. Nudging local models toward `search_tools`
  first (rather than guessing a schema lookup by name) might reduce this —
  worth checking whether the system prompt already says this only for
  cloud-model-sized context, since a 14B model may need a much more explicit
  instruction than a 400M-parameter-equivalent worth of subtlety.

#### 1e. Gemini prompt caching never actually engages

Every `prompt_cache` log line for Gemini reads `eligible: false, reason:
prefix ~4xx tok < 4096 tok floor for gemini-3.6-flash`. The system
prompt/prefix here is ~400–440 tokens — nowhere near Gemini's 4096-token
minimum for caching to kick in at all. That means **every single Gemini round
pays full price for the whole system prompt**, with zero savings, which
compounds problem 1a (10 keys burning through quota fast). This may just be
inherent to how compact this particular system prompt/tool-set is for this
conversation — worth checking whether it's worth padding the cached prefix on
purpose to clear the floor, or whether that floor makes caching simply
not worth pursuing for Gemini specifically at Jarvis's current prompt sizes.

#### 1f. A user typo got echoed faithfully into failing tool calls (minor)

The original message contains `D:\MyDigitalVault\prjects\...` (missing the
"o"). The very first `read_file` call reused that exact misspelled path
verbatim and got `does not exist`; a second attempt (different key) tried the
same typo'd path again. Later calls used the correct "projects" spelling but
still couldn't find the file via `search_files` (0 results) or `present_file`
("path not found") — the user ultimately had to supply the full nested path
themselves (`...\jarvis-cli\jarvis\cli.py`). This is a minor, lower-confidence
observation — it's not clear whether the file genuinely wasn't indexed by
Everything at that path or something else was wrong — but a model that
double-checks an unusual/broken-looking path against the user's own message
before retrying it verbatim (or that tries a search first instead of a direct
`read_file` on an unconfirmed path) would have avoided at least the first two
wasted calls.

**Correction (2026-09-20, see F.13):** `prjects` is the real folder name in
this vault — `list_dir` and `dir` succeed on it, and a saved memory and a saved
command both use it. The "typo" reading above is wrong; the original failure was
a wrong *nested* path. Don't build path-sanity logic on that premise.

#### 1g. Supplied 2026-09-22 JSONL — 100k+ input-token burn in a repetitive turn

This is a **separate trace from the older ~25-minute / 14-key log above**.
The supplied `ee8560f14376332f.jsonl` contains **376 log rows, 77 requests,
77 responses, 54 tool-call/result pairs, and 70 usage records**. The usage
records report **123,368 input tokens** and **2,027 output tokens**. The
strongest signal is not model output length; it is repeated input context.
The exact user message `write_on_screen continue and enter` appears **572
times inside request payloads**, because later requests keep carrying prior
conversation/tool material forward.

The trace also shows a recurring tool-discovery/action loop: `search_tools`
and `get_tool_schema` calls, attempts to invoke `desktop:write_on_screen`,
and repeated retries after the user had explicitly declined the action. Some
of those behaviours have already been addressed elsewhere in this plan, so
this new item should **measure the remaining context-growth problem rather
than assume every token is caused by one bug**.

**Research/implementation target:** keep the useful state needed to continue a
turn while preventing unchanged history from being recopied indefinitely.
The plan should investigate, then test, at least these boundaries:

- compacting repeated unchanged user turns and already-consumed tool results
rather than replaying the same payload wholesale;
- deduplicating tool schema/discovery material when the same tool is queried
again in the same logical turn;
- terminating or collapsing retries after an explicit user cancellation/decline
so the model is not asked to repeat a refused action; and
- preserving the final actionable state/results while trimming the redundant
transcript around them.

**Baseline for regression measurement:** 123,368 reported input tokens /
2,027 output tokens / 70 usage records from the supplied log. A successful
fix should make repeated turns stop showing monotonic input growth from
unchanged context, while preserving the tool/action semantics that the trace
was supposed to exercise.

#### Summary of what's provably true from this one log

| # | Finding | Confidence |
|---|---|---|
| 1a | 10 Gemini keys + 4 Groq keys exhausted in one turn | Definite (log shows it directly) |
| 1b | Capacity mode escalates 100%→150%→400% mid-turn on local models | Definite; causal link to the multi-minute gaps is a strong inference |
| 1c | `tool_choice:"none"` + tools still attached → 400, retried across keys pointlessly | Definite |
| 1c | `spawn_subagent`'s `parent_id` schema rejects the `null` the model sent | Definite, exact schema location identified |
| 1d | Local models narrated tool use instead of calling tools, 10 rounds, no auto-recovery | Definite (log shows zero real tool_calls in the whole Ollama phase) |
| 1e | Gemini prompt caching never engages (prefix under the 4096-token floor) | Definite |
| 1f | User's own path typo got reused verbatim by the model | Definite; whether `search_files` "should" have found the real file is unconfirmed |

---

### 2. Bug: Tesseract "on PATH" but OCR still fails

**Status: the diagnosis bug is fixed — `jarvis-ocr-diagnosis-fix.patch`
(rev. 2026-09-20p, in `jarvis-main_36_`). The remaining blocker is Tesseract
not being on the PATH of the process that runs the server.** Suggested
wording for the one-line record: *diagnosis bug fixed in
`jarvis-ocr-diagnosis-fix.patch`; the remaining blocker is Tesseract not being
on the PATH of the process that runs the server.* So the "no code change
needed" verdict below was half right: the suspected stale-PATH cause is still
the most likely *environment* cause, but Jarvis's own error hint was also
misleading (it blamed the wrong thing, and told the user to reopen a terminal
when the server needed restarting). Now the error names the real case, so the
"ask for the exact `(detail)` text" step below is no longer needed — read the
`cause`/`fix` on the failed call.

**Getting Tesseract onto PATH (Windows).** A process inherits PATH when it
starts, so the server has to be relaunched after PATH is fixed:

1. Check that `C:\Program Files\Tesseract-OCR\tesseract.exe` exists.
2. Add `C:\Program Files\Tesseract-OCR` to your **User** PATH.
3. Open a brand-new PowerShell window.
4. Confirm `where tesseract` finds it there.
5. Start the server from that same window with `node server.js`, or run
   `start-server.bat` from it. **Don't double-click the file** — that keeps
   Explorer's old PATH.

If `where tesseract` prints nothing, install it first:
`winget install UB-Mannheim.TesseractOCR`. If Tesseract is found but OCR still
fails with a language-data error, set `TESSDATA_PREFIX` to the folder that
holds `eng.traineddata`. (`jarvis doctor` from the same new terminal is the
quick cross-check.)

`ocr_tools.py` already does the right things here: it distinguishes a missing
`pytesseract` pip package from a missing Tesseract *binary*
(`_no_pytesseract()` vs `_no_tesseract_binary(detail)`, lines 64–69), and it
surfaces pytesseract's actual `TesseractNotFoundError` text in the `(detail)`
part of the error rather than a generic message. `doctor.py`'s binary check
(`check_binaries()`, line 235) uses `shutil.which("tesseract")` — a plain PATH
lookup, same mechanism pytesseract itself uses under the hood.

Given the user has confirmed it's on PATH and it *still* fails "no matter
what," the most likely causes, roughly in order of likelihood:

- **A long-running process has a stale PATH.** `web/server.js` spawns a fresh
  `python -m jarvis` child **per turn** (`spawnAndStream`, confirmed at
  `web/server.js:1938`/`2291`), but that child inherits its environment from
  the **Node.js server process**, which is itself long-running. If Tesseract
  was added to PATH *after* the Node server was last started (very easy to do
  — install Tesseract, then just keep using the already-open Jarvis web UI),
  every spawned Python child still carries the stale PATH the Node process
  captured at its own launch. Restarting the browser tab or even the Python
  CLI directly won't fix this — the **Node server itself** needs a full
  restart. This would also explain why `jarvis doctor` might report Tesseract
  as fine if run fresh from a new terminal, while the actual web-UI-triggered
  OCR calls keep failing — same machine, different process ancestry.
- **PATH scope mismatch.** If Tesseract was added to the *User* PATH but
  Jarvis (or whatever launches `web/server.js`) runs under a different
  context — a Windows service, Task Scheduler entry, or a different user —
  it may only see the *System* PATH.
- **Missing `TESSDATA_PREFIX` / language data**, a very common "tesseract
  binary is found but still doesn't work" cause unrelated to PATH at all —
  worth confirming the exact exception text pytesseract raises (which
  `_no_tesseract_binary`'s `(detail)` should already be showing) actually
  says "not found on PATH" and not something else like a missing
  `eng.traineddata`.

**Suggested next diagnostic step** (no code change needed): ask for the exact
`(detail)` text from a real failed `click_on_text`/`read_screen` call — the
existing error message should already say whether it's genuinely a
`TesseractNotFoundError` (→ PATH/process issue) or something else entirely
(→ different fix). If it is `TesseractNotFoundError`, fully closing and
restarting whatever process launches `web/server.js` (not just the browser)
is the first thing to try.

---

### 3. Feature: filter tool source in the Debug menu

**Status: implemented — `part_e_and_sec3.patch` (rev. 2026-09-21d).** The
MCP question below was checked directly against the code (still true:
`mcp_client.py`/`mcp-tools` are not merged into `TOOL_SCHEMAS`, so there is
no fourth bucket yet) and the "possible plan"'s three steps were followed
essentially as written. The rest of this section is left as originally
written for the "why" — what actually shipped:

- `tools_list_payload()` now returns `"source": "builtin"|"auto"|"user"`
  per tool, computed by a new `_tool_source()` helper exactly as step 1
  proposed (`"user"` if `name in USER_TOOL_NAMES`, else `"auto"` if the
  name came from the shipped `jarvis/actions/` scan, else `"builtin"`) —
  implemented as a set difference (`AUTO_TOOLS` names minus
  `USER_TOOL_NAMES`) rather than slicing by `_user_scan_start`'s index,
  since `_AUTO_VALID` has already dropped invalid records by that point
  and a positional split would land on the wrong boundary once anything
  is invalid.
- `debugFilteredTools()` in `app.js` now also filters on `source`; a
  segmented All/Built-in/Auto/User control sits next to `#debug-search`,
  same `.debug-toggle-btn` styling as the existing Organized/Raw toggle,
  plus a small source badge on each tool card.
- No MCP bucket was added — offering one before MCP tools actually reach
  this catalog would just always show empty, so the filter stays at three
  buckets until that's built.
- Covered by a new test, `tests/test_tool_source_filter.py` (6 checks):
  every tool gets one of the three known sources, no duplicate names, a
  known built-in and a known shipped-auto tool land where expected, and
  the three buckets partition the catalog with no overlap.

The rest of this section is left as originally written, for the "what was
missing / what a fix would need" analysis:

`tools_list_payload()` (`tools.py:728`) — the function behind `/api/tools`,
which feeds the Debug panel — returns only `name`, `description`,
`parameters`, `confirm_required`, `ai_review` per tool. **There is no
group/source field at all today**, and the Debug panel's own filtering
(`debugFilteredTools()`, `app.js:4967`) only matches against
`name`/`description` text. A source filter needs a field to filter *on*
first.

The good news: the codebase already cleanly distinguishes the sources that
would matter for a filter, they're just not exposed in this payload:

- **Built-in** — everything hand-wired into `TOOL_SCHEMAS`/`TOOLS` before the
  auto-discovery block (`tools.py:568`, `:768`).
- **Shipped auto-discovered** — `jarvis/actions/*.py`, the drop-in tool-file
  convention (`tool_loader.py`). These are `_AUTO_RECORDS` before
  `_user_scan_start` (`tools.py:858`).
- **User-authored** — `~/.jarvis/tools/*.py`, scanned second with the same
  loader. Already tracked explicitly as `USER_TOOL_NAMES` (`tools.py:870`),
  specifically so name-collision checks can tell "yours" apart from
  everything else — this set basically *is* the "user" filter bucket,
  already computed, just not exported over the API.
- **MCP** — worth double-checking whether MCP-bridged tools are even part of
  `TOOL_SCHEMAS`/`tools_list_payload()` at all; there's a separate
  `mcp-tools` CLI command and `mcp_client.py`, and nothing found in this pass
  wires MCP tools into the static catalog the Debug panel reads. If they're
  not in there today, "filter by source" can't offer an MCP option until
  that's addressed too — worth confirming before promising it as a filter
  choice in the UI.

**Possible plan:**
1. In `tools_list_payload()`, compute a `source` string per tool: `"user"` if
   `name in USER_TOOL_NAMES`, else `"auto"` if the schema came from
   `AUTO_TOOL_SCHEMAS` (and specifically the shipped-actions portion, not the
   user portion), else `"builtin"`. Add it to each item in the returned list.
2. Confirm/resolve the MCP question above; add a fourth bucket if applicable.
3. In `app.js`, extend `debugFilteredTools()` to also filter on this field,
   and add a small filter control (segmented buttons or a dropdown) next to
   `#debug-search` in the tool-list pane, following the same visual language
   as the existing `.debug-toggle-btn` organized/raw toggle.

---

### 4. Feature: Ollama thinking mode

**Status: implemented — `jarvis-ollama-openai-compat-thinking.patch` (see
§0.1b).** The rest of this section is left as originally written, for the
"what the log showed / what was likely wrong" analysis; what actually
shipped:

- `reasoning.py` gained `_looks_like_ollama_host(provider_name, base_url,
  model)` — true if the provider's `name` contains "ollama", its `base_url`
  is Ollama's default port (`:11434`) or ends in `/api/chat`, **or** the
  model string uses Ollama's registry `name:tag` shape with no `/` in it
  (an OpenRouter `org/model:variant` always has a `/`, so that heuristic
  can't false-positive on it). Any one signal is enough.
- When a provider typed `openai_compatible` matches that, `_openai_style()`
  now returns a fourth style (`"ollama_native"`, alongside the existing
  `effort`/`object`/`implicit`/`none`) and both `request_patch()` and
  `round_patch()` send `{"think": true}` for it instead of
  `reasoning_effort` — this is the actual fix, since Ollama silently
  ignores an unrecognized `reasoning_effort` field, which is exactly why
  `ollama1` in the log got no explicit thinking instruction at all.
- `extract_trace()`'s `openai_compatible` branch now also checks
  `message.thinking` (Ollama's own field name, same server underneath),
  alongside the existing `reasoning_content`/`reasoning`/`reasoning_details`
  checks, so a trace can come back too.
- This is the **code-fix branch** the original text below left as a choice
  ("if there's a real reason a second local instance needs to be
  `openai_compatible`... recognizing an Ollama-flavored `openai_compatible`
  provider... and sending `think` instead of `reasoning_effort` for it
  specifically") — reconfiguring `ollama1` to provider type `ollama`
  remains the simpler fix *if* nothing needs it on the OpenAI-compat
  endpoint specifically; this patch is what covers the case where something
  does.
- **Still unverified** (no network in the sandbox that built this): whether
  Ollama's `/v1/chat/completions` endpoint actually honors a top-level
  `think` key the way its native `/api/chat` does, and whether a real
  response comes back with `message.thinking` through that endpoint. The
  first live turn against a real `openai_compatible`-typed Ollama endpoint
  is the thing to watch — if `think` turns out to be silently ignored there
  too, this is a wash rather than a regression (previously-sent
  `reasoning_effort` was already being ignored), and the config-only fix
  (retype `ollama1` as `ollama`) is the fallback.

This one's partially already built, and the log surfaced a real, separate bug
in it.

**What already exists:** `reasoning.round_patch()` (`reasoning.py:566`) has an
explicit `ollama` branch (line 598) that returns `{"think": True}`, and
`CAPABILITIES` (line 87) lists `"ollama": True`. `ai_providers.py`'s
`call_ollama` calls `_apply_thinking(payload, provider, "ollama", ...)` each
round (line 1772). So the plumbing for "ask Ollama to think" does exist in
the code today.

**What the log shows:** thinking was never actually triggered in this
conversation — every request payload logged has no `think` key at all,
because the thinking level was off for this turn (unrelated to this bug, just
means this particular log can't confirm whether the *working* path actually
works end-to-end — that needs a separate test with thinking explicitly
turned on against the plain `ollama` provider).

**The actual bug:** the *second* configured local provider, `ollama1`
(`deepseek-r1:14b-qwen-distill-q4_K_M`), sent `"reasoning_effort"` in its
request payload — **not** `"think"`. `reasoning_effort` is the
`openai_compatible`-family parameter shape (`reasoning.py`'s
`_OPENAI_FAMILY_STYLE` table, for OpenAI/xAI/Groq-style providers), which
strongly suggests `ollama1` is configured with provider **type**
`openai_compatible` rather than `ollama` — pointed at Ollama's
OpenAI-compatible endpoint instead of its native one. `ADAPTERS`
(`ai_providers.py:1844`) treats these as two completely separate adapter
functions (`call_openai_compatible` vs `call_ollama`); a provider typed as
`openai_compatible` will never hit the `ollama`-specific `round_patch` branch,
no matter what it's actually running. An Ollama server likely just silently
ignores an unrecognized `reasoning_effort` field, so this provider gets no
explicit thinking instruction at all — the `reasoning` field that *did* show
up in its responses is almost certainly deepseek-r1's own default behavior
(R1-distilled models tend to reason unprompted, and Ollama auto-splits that
into a `reasoning` field for compatible models) rather than anything Jarvis
requested.

**Possible fix:** this may be a *configuration* fix rather than a code
fix — reconfigure `ollama1` with provider type `ollama` instead of
`openai_compatible` so it gets the correct `{"think": true}` treatment (and
the native `/api/chat` request shape generally, rather than the OpenAI-compat
passthrough). If there's a real reason a second local instance needs to be
configured as `openai_compatible` (e.g. a proxy in front of it, or a
non-Ollama OpenAI-compatible local server that happens to be named
"ollama1"), then the fix belongs in `reasoning.py` instead — recognizing an
Ollama-flavored `openai_compatible` provider (e.g. by hostname/model name
heuristics, or a config flag) and sending `think` instead of
`reasoning_effort` for it specifically.

---

### 5. Feature: model can still call tools after talking to the user

Confirmed as a real, precise gap, and worse than just "unsupported" — **the
current code actively discards interim text when it arrives alongside a tool
call in the same response.**

Every provider adapter in `ai_providers.py` follows the same shape:

```
if tool_calls_present and ...:
    # execute tools, append to history
    continue
text = "".join(text parts)
return AIResult(True, text=text, ...)
```

The `text = ...` extraction line is only ever reached when there were **no**
tool calls in that response. Concretely:

- **Anthropic** (`ai_providers.py:1180–1210`): `blocks = data.get("content")`,
  `tool_use_blocks = [...]`. If `tool_use_blocks` is non-empty, the branch at
  line 1183 runs and `continue`s — the `text = "".join(b.get("text",...))`
  line at 1205 is simply never reached for that response, even though Claude
  routinely returns a text block ("Let me check that...") *alongside*
  a tool_use block in the same message.
- **Gemini** (`ai_providers.py:1538–1571`): identical shape —
  `call_parts = [functionCall parts]`; if non-empty, line 1541's branch runs
  and `continue`s before the `text = "".join(...)` line at 1570 is reached.
  Gemini responses in the log itself show this exact combination happening
  (a `"thought": true` text part *and* a `functionCall` part in one
  response) — the non-thought text portion, if there were any, would be
  dropped the same way.

So today: any text the model generates in the same turn as a tool call is
silently thrown away, not merely "not streamed." The user only ever sees text
from the *final* round, the one with zero tool calls.

**What "just like any real AI agent" likely means:** modern agent-style APIs
(Claude, GPT-4/5's Responses API) treat text and tool calls as ordinary
content blocks that can coexist and interleave across a turn — the model
narrates ("I'll look that up now") *and* calls a tool, and a well-built
harness shows the narration immediately rather than waiting for a final
tool-free response to have anything to show at all.

#### Required behavior (explicit)

**Talking does not end a turn. Only the model's finish signal does.** A model
must be free to send text *and then* keep working — say "I'll look that up",
call a tool, read the result, say more, call another tool — for as many rounds
as it needs. The turn is over only when the provider reports that the model
itself is finished with the prompt *and* there are no tool calls pending.
"This response contained text" and "this response contained no tool calls" must
stop being treated as the definition of "done".

The finish signal differs per provider, so each adapter must read its own (the
shared loop then only sees a normalized `finish: "tool" | "done"`):

| Adapter | Turn continues (model wants more) | Turn is finished |
|---|---|---|
| `anthropic` | `stop_reason: "tool_use"` | `stop_reason: "end_turn"` |
| `openai_compatible` (OpenAI, Groq, xAI, OpenRouter, DeepSeek, Mistral, ...) | `finish_reason: "tool_calls"` / `tool_calls` present | `finish_reason: "stop"` |
| `gemini` | any `functionCall` part present (Gemini reports `STOP` even when it wants a tool, so check the parts, not just `finishReason`) | no `functionCall` part and `finishReason: "STOP"` |
| `cohere` | `finish_reason: "TOOL_CALL"` | `finish_reason: "COMPLETE"` |
| `ollama` | `message.tool_calls` present (no tool-specific `done_reason` exists) | `done: true` with no `tool_calls` |

Non-natural endings (`max_tokens`/`length`/`MAX_TOKENS`, refusals, content
filters, and the harness's own `MAX_TOOL_ROUNDS` / `round_budget` caps) stay
the hard stops they are today — but they are *forced* endings and should be
reported as such, rather than looking like the model finished on its own.

Consequences for the code:

1. **Never discard the text branch when tool calls are present.** Surface it
   (stream it — see §8) *before* running the tools, keep it in the saved
   conversation as its own interim assistant message, and append it to the
   provider-native history alongside the tool calls (Anthropic already appends
   the whole `blocks` list, so that half is done; the OpenAI-shape adapters
   need the assistant message to carry `content` *and* `tool_calls`).
2. **Loop on the finish signal**, not on "were there tool calls this round".
3. **Keep §1d separate.** A local model that says "I will now search..." and
   then returns `finish_reason: "stop"` with no tool calls *has* finished, by
   this rule. Deciding whether that finished turn was a real answer or a
   non-answer is the §1d recovery layer's job — don't fold it into the finish
   rule, which should stay purely provider-driven.
4. This changes what a "turn" is for every provider, so prototype it on one
   adapter first (Anthropic has the clearest signal) — but it is not done
   until all five adapters follow it.

**What's actually implemented (rev. 2026-09-21b, `phase-a-partA-56.patch`) —
Anthropic only; this is the prototype step, not the finished item.**
`call_anthropic` now extracts `text` unconditionally every round (before, that
line was only reached with no tool calls, so text sent alongside a `tool_use`
block was never extracted at all). When text co-occurs with `tool_use_blocks`:

1. It is captured by the new `ai_providers.get_interim_text()` — a per-attempt
   list of `{"round": N, "text": "..."}`, reset in `set_thinking()` (same
   lifecycle as the thinking-trace collector beside it).
2. It is fired live, **before the tools run**, through a new optional
   `on_interim_text(text, round_num)` hook, threaded via `set_log_context()`'s
   thread-local (the pattern `on_tool_usage` already uses, so no adapter call
   signature widened). A raising hook never breaks the turn.
3. `ai_client.ask()` gained `on_interim_text=` (passed on both the initial
   attempt and the D5 short-429 retry); `cli.py` wires it to an always-on
   stderr line `» <text>`, matching the no-debug-flag convention of
   `on_tool_call` / `on_tool_result` / `on_route`. `server.js` already forwards
   stderr live, so it reaches the browser console too.
4. It is persisted in the saved conversation as an `"interimText"` extra
   (`{"items": [{"round": N, "text": "..."}]}`), same pattern as `"thinking"`.
   The final answer stays clean of it.

**Done on all five adapters (rev. 2026-09-22a, `jarvis-finish-signal.patch`,
merged with `jarvis-partC-D2.patch` — see §0.1f).** Gemini, OpenAI-compatible,
Cohere and Ollama now extract and surface text alongside tool calls the same
way Anthropic does, through the shared `_surface_interim_text()`. The
normalized `finish_signal(reason, has_calls, atomic_calls=False)` — returning
`finish: "tool"|"done"` and `cut: None|"length"|"refused"|"filter"` —
replaces "were there tool calls this round" as the loop condition on every
adapter, per the table in "Required behavior" above. Gemini's atomic
function-call parts (`atomic_calls=True`) survive a `MAX_TOKENS` cut; the
other four providers' calls can be cut off mid-argument, so a cut that lands
on a tool call is `KIND_CUTOFF` and that call is never run. A cut that lands
on a plain-text answer instead sets `AIResult.cut`, so `ai_client.ask()`
reports it (`AskResult.ending = "truncated"`) rather than presenting it as a
finished reply. **This is still narration-level surfacing** (whole interim
message, once complete), **not** token streaming — that is §8, which depended
on this landing on every adapter and can now start. Tests:
`tests/test_interim_text.py` (20 checks, Anthropic-only, from rev. 2026-09-21b)
plus `tests/test_finish_signal.py` (121 checks covering the `finish_signal`
table for all five providers, interim text live on every adapter, Gemini's
`thought` exclusion, Cohere's `tool_plan`+`content` combination, truncated
calls never running, cut-off answers reported as such, and "talking does not
end a turn"). Not run against real provider keys.

**Known gaps closed (was 2026-09-21c's cross-check, §0.5 item 4):** `app.js`'s
`renderThreadExtra` now has an `"interimText"` case, so it survives a reload;
the live `»` stderr line's cut was raised from 240 to 600 characters.

**Still open:** §8 (true token streaming) — depends on §5, which is now
finished on every adapter.

---

### 6. Feature: thinking between tool calls

Directly explained by `_apply_thinking()` (`ai_providers.py:141`):

```python
if round_num != 0 and not (ran_tools and thought_rounds < 2):
    return False
```

Thinking is applied at round 0 (always), and then **at most one more time** —
the round immediately following a tool batch — capped by `thought_rounds < 2`.
After that cap is hit, every subsequent round in a longer tool-calling
sequence gets no thinking patch at all, regardless of how many more tool
calls are still ahead. So today's behavior is closer to "think once at the
start, and once more after the first tool round" than "think before every
tool call" — this cap is exactly the number to change (or replace with a
different policy) for the feature as described. Worth deciding deliberately
what the new policy should be (every round? every round up to some higher
cap? scaled by thinking level, e.g. `high` gets more thinking rounds than
`low`?) rather than just raising `2` to a bigger constant, since each
additional thinking round has a real token/latency cost per the same budgets
already defined in `_LEVELS` (`reasoning.py:72`). Whichever policy is chosen,
§8 shows the result live: every round that carried a thinking request gets its
own thinking block streamed in the UI, in order with the text and tool calls
around it.

**What's actually implemented (rev. 2026-09-21b, `phase-a-partA-56.patch`).**
Done, on all five adapters (the change is in shared `_apply_thinking`). The
cap now comes from `reasoning.max_thinking_rounds(level)` — a new
`max_thinking_rounds` field on `_LEVELS`: `low` → 2 (unchanged; a default
policy choice, keeps the cheapest level's cost as-is), `medium` → 4, `high` →
uncapped (every round that ran tools gets a thinking request). Round 0 always
applies and the `ran_tools` gate still holds. **Caveat:** `round_patch`'s own
`budget_for_round`/`MIN_USEFUL_BUDGET` floor independently zeroes `low`'s
post-round-0 budget on Anthropic/Gemini (`1024 * 0.4 = 409`, under the `1024`
floor). That is orthogonal to this item and was left alone, so `low` on
Anthropic/Gemini is unchanged in practice; the visible difference is at
`medium`/`high`, and at `low` on OpenAI-compatible-style providers (which use
`effort_for_round`, no floor). Tests: `tests/test_thinking_round_cap.py`, 12
checks (monkeypatches `round_patch` to always return a non-empty patch so only
the cap logic is exercised).

---

### 7. Bug: auto-discovered tools don't show `routed: x`

**Status: implemented — `jarvis-auto-discovered-tool-keyword-fallback.patch`
(see §0.1b).** Went with the first of the two "possible fix directions"
below (auto-derive a minimal keyword set), not the second (a separate
`search_tools`-found trace line) — that second option is still open if it's
still wanted alongside this one; they're not mutually exclusive. What
actually shipped:

- `tool_loader._validate()` now calls a new `_derive_fallback_keywords(name)`
  for any tool name in a file that has no explicit `TOOL_KEYWORDS` entry
  (or an empty/falsy one). It splits the name on `_`, drops words under 4
  characters and a small stopword list (generic verbs/fillers like "list",
  "tool", "show", "check", ...), and gives every surviving word a weight of
  exactly 5 — matching `tool_router.MIN_SCORE`, the lowest weight that
  still counts as real router signal (see `tool_router.py`). A name made
  entirely of short/stopword words (e.g. `get_all`) derives nothing, same
  as before.
- This never overrides a real, human-written entry — only names with zero
  keywords of their own get a derived one, and a file where every tool
  already has keywords is untouched (no log line at all).
- The old "loud warning" (`it will never be reachable through
  tool_router.route()...`) now only fires when even the fallback can't
  help (e.g. every name in the file is stopword-only). When a fallback was
  successfully derived for one or more tools, a softer `[tools] Note:`
  line lists which ones, and still encourages real `TOOL_KEYWORDS` for
  better phrase coverage than a bare name gives.
- Verified end-to-end against `tool_router.route()` (not just that a dict
  entry exists): a fallback-only tool's derived keyword actually clears
  `MIN_SCORE` and activates its group for a matching message.
- This is a floor, not a substitute for real keywords — `spotify_search`
  derives `{"spotify": 5, "search": 5}`, which is real but far weaker
  signal than a purpose-written phrase a person would actually type. Every
  currently-shipped action file that had zero `TOOL_KEYWORDS` now gets at
  least this (confirmed against the real `jarvis/actions/*.py` tree while
  testing: `code_agent.py`'s `edit_file`/`read_file`/`run_shell`/
  `search_code` and `workspace_tools.py`'s `daemon_status` all picked up a
  derived fallback where they previously had none).

This isn't a bug in the sense of broken code — it's a real gap in the
`routed:` trace, but it's structural, not accidental. The `routed:` line
(`cli.py:785`, human-readable version in `turn_trace.py`) only reports on
`tool_router.route()`'s output — a purely keyword-based system scored against
`TOOL_KEYWORDS`/`TOOL_GROUPS` (`tool_registry.py`, built from
`tools.TOOL_SCHEMAS` + `tools.AUTO_TOOL_GROUPS`/`AUTO_TOOL_KEYWORDS`).

The relevant detail is in `tool_loader.py`'s own contract docstring: a
drop-in tool file under `jarvis/actions/` (or `~/.jarvis/tools/`) **must**
declare `TOOL_GROUP`, but `TOOL_KEYWORDS` is explicitly *optional*. A tool
file with a `TOOL_GROUP` but no `TOOL_KEYWORDS` contributes tools that are
correctly categorized but have nothing for `tool_router.route()`'s
substring/word-boundary matching (`tool_router.py:92–128`) to ever match
against — so that tool's group can only ever get selected by *another* tool
in the same group having a matching keyword (in which case the trace credits
that other, keyworded tool, not the auto-discovered one — so it looks like it
"doesn't show routed: x" even though a routed: line did technically fire, just
without naming it), or the tool is never reached via the router at all and
only ever surfaces through `search_tools`/the full-catalog fallback — which
*is* traced, just as its own separate step ("looked through my own tool
catalogue"), not as a `routed:` line, since it structurally isn't a router
decision.

**Possible fix directions:**
- **Encourage/require keywords for auto-discovered tools** — since the
  contract already supports `TOOL_KEYWORDS` optionally, either nudge tool
  authors harder (docs/template comment in `actions/_template.py`) to always
  provide a few, or auto-derive a minimal keyword set from the tool's own
  name/description at discovery time as a fallback so every auto-discovered
  tool gets *some* router coverage instead of none.
- **Give `search_tools`-found tools their own equivalent trace line** —
  rather than trying to force every auto-discovered tool through the
  keyword router, make `turn_trace.py` render a comparable "found via search:
  X (query 'Y')" line when a tool was reached through `search_tools` instead
  of `route()`, so auto-discovered tools get *equally visible* provenance,
  even though the underlying mechanism is legitimately different from
  keyword routing.

---

### 8. Feature: real-time streaming of text AND thinking, on every provider

#### 8.0 What is wanted (explicit requirements)

Jarvis should stream exactly like any normal chatbot/agent (ChatGPT, Claude,
Grok, the Ollama CLI...). Concretely:

1. **Text streams word by word, in real time**, as the model generates it —
   not as one block after the whole response is done.
2. **Thinking streams the same way.** When a model thinks, the thinking text
   appears live, word by word, in its own block — the way Anthropic, Ollama,
   ChatGPT and Grok show it — not as a finished blob that shows up after the
   turn.
3. **The thinking is user-toggleable.** The user can choose whether to *see*
   the thinking process or not (show/hide), like any AI agent. It is a
   display switch: it works instantly, also in the middle of a turn, and
   never changes what the model is asked to do (that is the separate thinking
   *level*, see 8.5).
4. **Streaming exists in EVERY provider adapter** — `anthropic`, `gemini`,
   `openai_compatible` (and so every provider typed that way: OpenAI, Groq,
   xAI/Grok, OpenRouter, DeepSeek, Mistral, Together, Perplexity, Cerebras,
   Ollama-via-OpenAI-compat), `cohere` and `ollama`. No adapter may keep a
   blocking request. A provider that has no thinking to show (see 8.3) still
   streams its text.
5. **Text and tool calls interleave.** The model can talk, run tools, talk
   again and run more tools — and the turn only ends when the model's own
   finish signal says it is done with the prompt (the rule and the per-provider
   signals are in §5). Streaming has to respect that: text that arrives
   before a tool call is shown immediately and kept, not held back or thrown
   away.
6. **It should feel like a normal agent**: live status ("thinking…",
   "writing…", "running <tool>…"), markdown rendered as it grows, auto-scroll,
   a working Stop button mid-generation, and the plain CLI printing
   incrementally too.

#### 8.1 What exists today (verified against the source)

**Nothing streams — text or thinking.**

- Every adapter makes one blocking request. Gemini uses `:generateContent`
  (`ai_providers.py:1387`, not `:streamGenerateContent`); Ollama sends
  `"stream": False` (`:1771`); the shared `_post_json()` (`:308`) is a plain
  blocking `requests.post(...)` with no `stream=True`; there is no SSE/NDJSON
  parsing anywhere for any of the five adapters.
- **Thinking is captured only after the fact.** `_apply_thinking()` (`:141`)
  asks for thinking per round; `_collect_thinking()` (`:189`) then extracts the
  text from the *completed* response (`reasoning.extract_trace`) into
  `_thinking["trace"]` (capped at 20 000 chars). `ai_client.ask()` saves it as a
  `thinking` extra once the whole attempt succeeded (`ai_client.py` ~2750).
  Gemini's thought parts are actively filtered out of the answer text
  (`ai_providers.py` ~1545–1571). So even where the model thinks, the user
  can only ever see it *after* the turn.
- **The show/hide toggle already exists — but only for the finished trace.**
  The web UI has a thinking picker with the levels off/low/medium/high plus a
  **"Show reasoning trace"** item (`app.js` ~4815–4870, `thinkState.show`,
  persisted through `POST /api/think`, which wraps the CLI's `jarvis think`).
  It only controls whether a trace bubble is added to the thread after the
  answer. The plan below **reuses this exact toggle** instead of inventing a
  second one, and makes it govern the *live* thinking block.
- **The transport is already shaped for this — better than first assumed.**
  The CLI→server.js→browser path already carries structured events as
  *marker lines* on stdout: `JARVIS_CONFIRM_REQUEST {...}` and `JARVIS_USAGE
  {...}` (`server.js:2085`, `:2091`), peeled off in `onStdoutLine`
  (`server.js:2265`) and re-sent as WebSocket messages (`ask-confirm-request`,
  `ask-usage`). `spawnAndStream` (`:1935`) already sets `PYTHONUNBUFFERED=1`
  and reads line by line via `makeLineBuffer` (`:1903`). Streaming is one more
  marker of the same family — a small addition, *not* zero work (an earlier
  version of this doc said server.js needed almost nothing; it needs a new
  marker branch).
- **The browser only renders once.** `ask-stdout` lines are pushed into
  `state.askReplyLines` (`appendAskReplyLine`, `app.js:4012`) and the reply is
  split and rendered as markdown in `finalizeAskBubble` (`:4020`) when
  `ask-exit` arrives.

#### 8.2 The moving parts, layer by layer

**1. A shared streaming helper — `_post_stream()` next to `_post_json()`.**
Same never-raises contract and the same error collapsing (timeout /
connection / HTTP status via `_status_reason`), but `requests.post(...,
stream=True)` and an iterator over decoded events. Notes:
- `timeout` becomes a *between-chunks* read timeout: a slow local model that is
  steadily producing tokens is no longer indistinguishable from a hang. (This
  is exactly the visibility problem in §1b/§1d — multi-minute silences on local
  models become visible progress.)
- Log the assembled request/response once per round (same `logs.log` shape as
  today), not one log line per chunk.
- Closing the response on abort must stop generation on the provider side too.

**2. One normalized event vocabulary, emitted by every adapter.** Nothing above
the adapter should know which provider it is talking to:

| Event | Meaning |
|---|---|
| `text` | a text delta |
| `thinking` | a thinking delta |
| `tool` | a tool call is being made (name, and arguments once complete) |
| `round_end` | one model response finished; `finish: "tool"` (run tools, loop) or `"done"` (turn over) — see §5 |
| `reset` | discard what was streamed for this attempt (mid-stream failover, see 8.6) |

Each adapter still returns the same `AIResult` at the end (assembled text,
`usage`, `tool_history`), so `ai_client.ask()`'s failover, usage bookkeeping,
the saved conversation, tests and `benchmark_pc_actions.py` keep working.

**3. How events reach the caller.** Through a module-level sink, set per attempt
and cleared in the same `finally:` as the log context — the exact pattern of
`set_thinking()` / `set_log_context()` (`ai_providers.py` ~108–136). The
comment there explains why: `ADAPTERS[type](provider, messages, timeout, ...)`
is a uniform contract other callers use, and widening five signatures would
break them. So: `set_stream_sink(callback)` from `ai_client.ask()`; adapters
call it as chunks arrive. Keep a single config escape hatch
(`defaults.stream`, **default on**) to fall back to the blocking path for
debugging or a proxy that mangles streams.

**4. `ai_client.ask()`.** Wires `on_delta`/sink from the CLI into the sink above,
in the same way `on_tool_call` / `on_trace` are already passed through.

**5. CLI (`cli.py`).**
- **Terminal (TTY):** print text deltas straight to stdout as they arrive;
  print thinking deltas dimmed on stderr, only when the show-thinking setting
  is on (same setting as `jarvis think`, single source of truth).
- **Piped (what `server.js` uses):** emit one marker line per event,
  `JARVIS_STREAM {"k":"text","d":"..."}`, `flush=True`, matching the
  `JARVIS_USAGE` convention. The JSON is one physical line (newlines inside a
  delta are escaped), so `makeLineBuffer` can never split an event and needs no
  change. Coalesce deltas in Python (~30–50 ms) rather than sending one line per
  token. Still print the full assembled reply at the end as today, so the
  existing exit/persist path and every other command's
  one-JSON-blob-on-stdout contract (`tools-list` etc.) are untouched.

**6. `web/server.js`.** Add a `JARVIS_STREAM ` branch to `onStdoutLine`
(`:2265`), same shape as the `USAGE_MARKER` branch, forwarding as
`{type: "ask-stream", ...}`. Abort already kills the child (`server.js` ~2119),
which now also stops mid-generation.

**7. `app.js`.** New `case "ask-stream"` next to `ask-stdout` (`:2574`), and a
*segmented* assistant message instead of one bubble — see 8.5.

#### 8.3 Per-provider streaming map (every adapter is in scope)

| Adapter (line) | Covers | Turn on streaming | Text | Thinking | Tool calls | Finish / usage |
|---|---|---|---|---|---|---|
| `call_anthropic` (`:1075`) | Anthropic | `"stream": true`, SSE | `content_block_delta` → `text_delta` | `thinking_delta` (also accumulate `signature_delta`: the adapter replays thinking blocks with their signatures on the next round, `:~1184`, so the reassembled `blocks` list must be identical to the non-streamed one) | `input_json_delta` fragments per `tool_use` block | `message_delta.stop_reason` (`end_turn` / `tool_use`); usage in `message_start` / `message_delta` |
| `call_gemini` (`:1379`) | Gemini | `:streamGenerateContent?alt=sse` instead of `:generateContent` (`:1387`) | `parts[].text` where not `thought` | `parts[]` with `thought: true` (needs `includeThoughts`, which `round_patch` already sets); today these are filtered out — instead, stream them | `functionCall` parts (arrive whole) | last chunk's `finishReason` **plus** presence of `functionCall` (see §5); `usageMetadata` on the last chunk |
| `call_openai_compatible` (`:862`) | OpenAI, Groq, xAI/Grok, OpenRouter, DeepSeek, Mistral, Together, Perplexity, Cerebras, Ollama's OpenAI-compat endpoint | `"stream": true` (+ `stream_options: {"include_usage": true}` where supported), SSE | `delta.content` | `delta.reasoning_content` (DeepSeek/Groq/xAI style) **or** `delta.reasoning` (OpenRouter / Ollama-compat style) — read both. This is also what makes an `ollama1`-style provider typed `openai_compatible` (see §4) show its thinking. | `delta.tool_calls[i]` fragments keyed by `index`; concatenate `function.arguments` until complete | `finish_reason` (`stop` / `tool_calls`); usage chunk at the end |
| `call_cohere` (`:1627`) | Cohere | `"stream": true` on `/v2/chat`, SSE | `content-delta` events | none today (`CAPABILITIES["cohere"]` is `False`); if Cohere reasoning models are enabled later, their thinking content blocks stream through the same `thinking` event | `tool-call-start` / `-delta` / `-end` | `message-end` (`COMPLETE` / `TOOL_CALL`) with usage |
| `call_ollama` (`:1731`) | Ollama (native `/api/chat`) | flip `"stream": False` → `True` (`:1771`); **NDJSON, not SSE** | `message.content` | `message.thinking` (present when `think: true`, already sent by `round_patch`) | `message.tool_calls` (arrive whole in a chunk) | final chunk `done: true`; usage in `prompt_eval_count` / `eval_count` |

Honest limits (provider-side, not Jarvis gaps): some providers do not expose
their raw reasoning at all (OpenAI's own reasoning models over Chat
Completions, currently; models that simply don't think). For those the text
still streams, and the thinking block shows only its "Thinking…" indicator —
there is no text to reveal. The feature must degrade to that cleanly, not
error.

Tool-call arguments arrive as partial JSON. Never execute a tool, or show its
arguments, until the fragments parse as complete JSON; show only "calling
`<name>`…" while they build.

#### 8.4 Text before a tool call (ties into §5)

> **Status (rev. 2026-09-21b):** §5's non-discarding half exists on Anthropic only (`get_interim_text()`, `on_interim_text`, `interimText` extra). §8 should build on that hook and needs the same on the other four adapters first.

The stream is one model response per round. Within a round the events can be
`thinking… text… tool`, and the round then ends with `round_end(finish="tool")`;
the next round starts with fresh `thinking…`/`text…`. Text that was streamed
before a tool call is **not** provisional or resettable — it is a legitimate
interim message: it stays in the UI, is saved in the conversation, and is sent
back to the provider as part of the assistant turn. The turn ends only on
`round_end(finish="done")`.

#### 8.5 Web UI behavior — like any AI agent

**One assistant turn = an ordered list of segments** rendered as they arrive:
`thinking → text → tool → thinking → text → tool → ... → text`. The current
single "pending bubble + trace bubble + lines joined at the end" model is
replaced by this.

- **Thinking block (collapsible).** While thinking is streaming: header
  "Thinking…" with a live indicator, body streaming word by word in a dimmer
  style. When the thinking segment ends: header becomes "Thought for 12 s" and
  the block collapses (click to expand), like Claude/ChatGPT.
- **The show/hide toggle** is the existing **"Show reasoning trace"** item in
  the thinking picker (`app.js` ~4823). It is a *display* toggle only:
  - ON → thinking blocks are visible and auto-expanded while streaming.
  - OFF → thinking blocks are hidden; only a small "Thinking…" status remains so
    the UI never looks frozen.
  - Applies instantly and mid-turn. Because thinking events are always
    forwarded to the browser whenever the model is thinking, turning it ON
    partway through reveals everything streamed so far — nothing is lost by
    having it off.
  - Persisted across reloads (already true via `/api/think`); saved
    conversations re-render their stored `thinking` extras collapsed/hidden
    according to the same toggle.
- **Thinking level is separate.** `off / low / medium / high` decides whether
  the model is *asked* to think and how much (tokens/cost, `reasoning.py`
  `_LEVELS`). Show/hide decides whether the user *sees* it. A model that
  reasons unprompted (deepseek-r1 distills, see §4) still streams thinking; the
  toggle governs its display.
- **Text segments.** Append deltas to the current text segment. Do **not**
  re-render the whole markdown on every token — the current
  `rerenderAskPendingBubble()` re-renders everything per call, which becomes
  O(n²) at token rate. Render at most once per animation frame, and tolerate
  half-finished markdown mid-stream (an unclosed code fence must not flash the
  rest of the message as code).
- **Tool segments** appear at the moment the call starts, with the existing
  tool/confirm UI, in order with the text around them.
- **Status line:** `thinking…` → `writing…` → `running <tool>…` → back.
- **Scrolling:** follow the stream only while the user is at the bottom;
  scrolling up must not be yanked back down.
- **Stop:** halts generation immediately, keeps what has streamed so far and
  marks the message as stopped.
- **Reconcile at the end.** Streamed content is provisional; the final assembled
  reply printed at exit is authoritative. On `ask-exit`, the final text
  replaces the streamed text so what is displayed and what is saved can never
  drift.

#### 8.6 Failure handling that streaming introduces

- **Mid-stream failover (ties into §1a).** A key can die *after* partial output
  was already streamed (429/503 mid-response, dropped connection). `ai_client`
  then moves to the next key/provider, so the sink must emit `reset` and the UI
  must discard (or grey out) that partial segment with a "switching to
  `<provider>`…" note. Otherwise the user sees half a sentence followed by a
  different model's full answer. `reset` only applies to the *failed
  attempt's current round*, never to text already committed before a tool
  call (8.4).
- **Post-hoc rejection.** `ai_client.ask()` currently rejects a finished reply
  after the fact (`_is_tool_trace_reply`, ~`:2738`), and adapters discard
  refusals / `content_filter` results. With streaming, either run those checks
  on the first N characters before releasing them to the UI, or emit `reset`.
- **Usage/logging/history keep their shape.** `get_usage_summary()` (needs the
  end-of-stream usage chunk from each provider — see the table),
  `tool_history` handed to the next provider, `logs.log` request/response
  entries, and the persisted `thinking` extra (still clipped to
  `max_trace_chars` for *storage*; the live stream itself is not truncated).

#### 8.7 Suggested order of work

Build the shared pieces once (`_post_stream`, event vocabulary, sink,
`JARVIS_STREAM` marker, segmented UI), then port adapters in this order —
Ollama native first (local, free to test, and it exercises text + thinking +
tools end to end), then `openai_compatible` (one adapter covers most cloud
providers), then Anthropic, Gemini, Cohere. The order is only a work order:
**the feature is not done until all five adapters stream**, and the
per-provider checks below pass for each.

#### 8.8 Acceptance checklist

- [ ] Text appears incrementally, word by word, on all five adapter types —
      `anthropic`, `gemini`, `openai_compatible`, `cohere`, `ollama`.
- [ ] Thinking streams live, word by word, for every provider that exposes it:
      Anthropic, Gemini, Ollama, and `openai_compatible` providers that return
      `reasoning_content` / `reasoning` (including an Ollama instance typed
      `openai_compatible`). Providers that don't expose thinking degrade to a
      plain "Thinking…" indicator, without errors.
- [ ] The show/hide toggle works instantly, including mid-turn; turning it on
      reveals the thinking already streamed; it doesn't change the thinking
      level or trigger any new request.
- [ ] A model can write text, call tools, write more text, call more tools —
      and the turn ends only on the provider's finish signal (§5), on all five
      adapters. Interim text is shown live and saved.
- [ ] Tool-call arguments are never executed or displayed until complete JSON.
- [ ] Failover mid-stream discards the failed attempt's partial output cleanly
      and shows which provider took over.
- [ ] Stop halts generation right away and keeps the partial message.
- [ ] Usage totals, logs, `tool_history`, and the saved conversation are
      identical in shape to today's; the saved text equals the final displayed
      text.
- [ ] Plain-terminal `jarvis ask` prints incrementally (thinking dimmed, only
      when show-thinking is on); commands that print a single JSON blob to
      stdout are unchanged.
- [ ] Reloading a saved conversation shows its interim messages and thinking
      blocks correctly, per the toggle.


---

# Part B — Clipboard tool (`clipboard_tools.py`)

> **CURRENT AUDIT — 2026-09-22:** Delivered. D7 is also delivered: `clipboard_watch_set_pattern` and `clipboard_watch_get_pattern` are model-visible. The deliberate invariant remains: the model may start/stop/status/list the built-in watcher through the existing daemon controls but cannot invent daemon argv. **New open work:** clipboard-watch currently emits one notification per matching clipboard change, so a bursty application can create a notification flood; this needs to be handled by the shared notification-importance/flood-control plan below. Remaining non-feature work is verification on real OS clipboard backends.


**Status: Delivered, in full.** `jarvis-clipboard-tool.patch` (the core
tools) plus `jarvis-clipboard-watch.patch` (the "Continuous watch" section
below — see the 2026-09-20g/h revision notes above for what's in each, and
2026-09-20h specifically for a deliberate deviation from this section's
own spec text around `daemon.add()`). Both apply independently of
F.3/F.1/F.8.

**For:** the implementing agent. **Effort:** small — this should be a short,
self-contained module, closer to `radio_tools.py` in size than `audio_tools.py`.

### Why this shape

Every existing tool module (`audio_tools.py`, `web_tools.py`, `git_tools.py`,
...) is a pair of module-level exports:

```python
CLIPBOARD_TOOL_SCHEMAS = [ {...}, {...} ]   # provider-agnostic JSON schemas
CLIPBOARD_TOOLS = { "clipboard_get": tool_clipboard_get, ... }  # name -> fn
```

`tools.py` imports both and folds them into `CORE_TOOL_SCHEMAS` /
the dispatch dict — see its imports (`from .audio_tools import
AUDIO_TOOL_SCHEMAS, AUDIO_TOOLS`, line ~45) and where `*AUDIO_TOOL_SCHEMAS`
gets spread into `CORE_TOOL_SCHEMAS` (line ~544). Follow that exactly:

```python
# tools.py
from .clipboard_tools import CLIPBOARD_TOOL_SCHEMAS, CLIPBOARD_TOOLS
...
CORE_TOOL_SCHEMAS = [
    ...,
    *CLIPBOARD_TOOL_SCHEMAS,
    ...
]
```
Once that's wired in, the Debug panel in the web UI picks it up automatically
(it lists and can invoke every registered tool) — no frontend work needed
for the basic read/write tools.

### Tools to expose

Keep the surface small, matching this codebase's philosophy of many narrow
tools over one do-everything tool (see `audio_tools.py`'s
volume_up/volume_down/set_volume/mute split rather than one `set_audio` tool):

- **`clipboard_get`** — no args. Returns `{"ok": true, "text": "...", "truncated": bool}`.
- **`clipboard_set`** — `{"text": "..."}`. Returns `{"ok": true}`.
- **`clipboard_clear`** — no args. Small enough to be worth its own tool
  rather than `clipboard_set({text: ""})`, since "clear the clipboard" is a
  common enough ask that the model shouldn't have to infer the empty-string
  trick.
- **`clipboard_wait_for_change`** — `{"timeout_seconds": 30}` (default ~20,
  cap ~120). Blocks the *tool call* (not a new asyncio task) polling every
  ~0.5s until the clipboard content changes from what it was when the call
  started, then returns the new text — or `{"ok": false, "timed_out": true}`.
  This covers the actual common "glue automation" need ("copy that URL, then
  I'll tell you when") without building a whole background-watch subsystem
  for it. See "Continuous watch" below for the separate, heavier feature.

### Cross-platform backend

Clipboard access needs no COM interop the way `audio_tools.py`'s Windows
volume control does — every OS has a simple CLI or one-call API. Shell out
per-platform, same `_ps()`-style "one small helper per OS, one dispatch
function" shape audio_tools.py uses, but far simpler:

```python
import platform, subprocess

def _get_windows():
    # Get-Clipboard -Raw preserves newlines; without -Raw PowerShell
    # returns a line array that quietly drops blank lines.
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        capture_output=True, text=True, timeout=5)

def _set_windows(text):
    # Set-Clipboard -Value takes the text via stdin to sidestep argv length
    # limits and quoting hell for anything with quotes/newlines in it.
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
        input=text, capture_output=True, text=True, timeout=5)

def _get_macos():
    return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)

def _set_macos(text):
    return subprocess.run(["pbcopy"], input=text, capture_output=True, text=True, timeout=5)

def _get_linux():
    # Try in order; the first missing binary raises FileNotFoundError, not
    # a clean "not found" — catch that per attempt (see below).
    # Wayland: wl-paste. X11: xclip -o -selection clipboard, then xsel as
    # a second fallback (some minimal DEs ship xsel but not xclip).
    ...

def _set_linux(text):
    # Same three candidates in the same order, as -i / --clipboard variants.
    ...
```

**Linux is the real complexity here**, not Windows or macOS: there is no
single universal clipboard binary, and headless boxes may have *none*
installed (no display server at all). Handle this as a capability check, not
a crash:

```python
_LINUX_GET_CANDIDATES = [
    (["wl-paste", "--no-newline"], None),
    (["xclip", "-selection", "clipboard", "-o"], None),
    (["xsel", "--clipboard", "--output"], None),
]
```
Try each with `subprocess.run(..., timeout=5)`, catching `FileNotFoundError`
specifically (binary not installed — try the next one) and letting any other
exception (a real error from a binary that *did* run) surface as the actual
failure. If every candidate is missing, return a clear error: `{"ok": false,
"error": "No clipboard utility found (tried wl-paste, xclip, xsel). Install
one, e.g.: sudo apt install xclip"}` — this is the single most likely support
question this feature will generate, so the error message should hand the
person the fix, not just say "failed."

Also worth an explicit check up front on Linux: if `$DISPLAY` and
`$WAYLAND_DISPLAY` are both unset, skip straight to the clear "no display
server, clipboard tools don't apply here (e.g. SSH session, headless
server)" error rather than spending 15 seconds timing out on three missing
binaries first.

### Truncation and a safety note

Mirror `web_tools.py`'s `FETCH_MAX_CHARS = 4000` pattern: cap what
`clipboard_get` returns (something like 8000 chars is plenty — clipboard
contents are usually short) and set `truncated: true` rather than dumping
an arbitrarily large blob into the model's context because someone copied
a whole log file.

One line worth in the tool schema's description, not enforced in code: the
clipboard is exactly where a password manager puts a password right before
someone pastes it somewhere. `clipboard_get`'s description should say
something like *"the clipboard may contain sensitive data the person copied
moments ago — don't restate its contents back verbatim in chat unless asked
to."* This is a prompt-level nudge, not a code-level filter (there's no
reliable way to detect "this text is a password"), consistent with how this
codebase generally treats model-facing guidance vs. hard enforcement
elsewhere (e.g. `tool_safety.py`'s confirm-gate is a hard enforcement point;
this is closer to the phrasing choices already made throughout the tool
descriptions in `audio_tools.py`).

### Continuous watch (a separate, bigger feature — don't build inline)

**Status: Delivered — with item 2's mechanism changed, item 3 replaced.
See the 2026-09-20h revision note for the reasoning; the short version is
below each item.**

"Watch the clipboard and notify me when it changes" is a genuinely different
shape from a blocking tool call: it needs to keep running after the ask that
requested it ends. Don't implement this as a `while True` loop inside a tool
function — that would block the whole ask indefinitely and die the moment
the process exits.

The right home for this is the **existing daemon infrastructure**
(`daemons.py`), not a new subsystem:

1. Add a tiny standalone script, e.g. `jarvis-cli/jarvis/clipboard_watch.py`,
   invocable as `python -m jarvis clipboard-watch --pattern <regex>`,
   that polls `clipboard_tools._get()` every ~1s in a loop and calls
   `notifier.notify(...)` (see `notifier.py`) whenever the content changes
   and (optionally) matches `--pattern`.
   **Done as specified** — one difference: the pattern is read from a
   small `clipboard_watch_config.json` (re-read every poll) rather than a
   `--pattern` CLI flag, since the daemon's argv is fixed (see item 2).
2. Expose a small CLI wrapper, `jarvis clipboard-watch-start [--pattern ...]`,
   that calls `daemons.add(...)` with that command — this reuses everything
   daemons.py already has for free: start/stop/restart, PID tracking,
   console log capture, crash detection, and the web UI's existing Daemons
   panel to see/stop it, with zero new UI work.
   **Done differently:** `clipboard-watch` is a pre-registered **built-in**
   daemon (like scheduler/discord/instagram) instead of something added at
   runtime via `daemons.add()`. Same end result — `jarvis daemon-start
   clipboard-watch` (or the model's existing `daemon_start` tool) brings it
   up, `jarvis clipboard-watch-config --pattern ...` sets the filter — but
   nothing ever calls `daemons.add()` for it. `daemons.add()` itself is
   untouched and still exists as the general (human/CLI-only) mechanism for
   a user's own daemons; this feature just doesn't route through it.
3. Add a `clipboard_watch_start` / `clipboard_watch_stop` tool pair in
   `clipboard_tools.py` that's a thin wrapper calling `daemons.add()` /
   `daemons.stop()` with a fixed, predictable daemon id (e.g.
   `"clipboard-watch"`) — so the model can turn this on/off conversationally
   without the person needing to know the Daemons panel exists.
   **Not done, on purpose — this is the deviation.** This is the exact
   shape `AGENTS.md` and `workspace_tools.py`'s docstring both call out as
   deliberately absent ("there is no `daemon_add` tool"), because it hands
   the model a way to register a daemon, even if this call site only ever
   points at one fixed script. Since `clipboard-watch` is a built-in (item
   2), the model already gets start/stop for it for free through
   `daemon_start`/`daemon_stop`, which is the actual conversational
   on/off this item wanted — just without a new tool that could register
   *arbitrary* daemons under a friendly name. See owner decision D7 above
   for the one part of this item genuinely left open: whether the model
   should also be allowed to set the watch pattern itself.

This is the same reasoning `subagents.py` already leans on for its own
background work (task_runner + the scheduler daemon) rather than each
feature inventing its own process-supervision code.

### Clipboard notification flood control — new open feature

The supplied implementation currently calls `notifier.notify()` whenever the
clipboard changes and the configured regex matches. Exact repeats are naturally
ignored because the watcher compares `current == baseline`, but there is no
debounce/coalescing window, per-watch rate limit, burst counter or flood summary.
Copy-heavy applications can therefore turn one human action into a stream of
toasts/DMs. The durable inbox cap (`MAX_INBOX = 200`) is a storage bound, not a
push-flood control.

The implementation should build on the **same `low` / `normal` / `high`
importance levels** used by the existing `digest.py`, not create a separate
clipboard-only priority vocabulary. Proposed semantics:

- **high:** immediate and never suppressed by the flood coalescer; use only
when the watch is intentionally configured for something that should interrupt.
- **normal:** delivered immediately, but rapid matching changes within a short
coalescing window are combined into one notification with a count and the most
recent preview.
- **low:** eligible for the existing digest batching; when digesting is off,
still respect the flood coalescing window rather than sending every event.

The watcher/config should gain a small, explicit flood-control policy (for
example coalescing window + optional burst cap), with safe bounded defaults and
no loss of the durable inbox record. When a burst is collapsed, the notification
should say how many events were coalesced and point to the inbox/history for the
individual events.

**Acceptance:** a synthetic burst of many matching clipboard changes produces
a bounded number of pushed notifications with a truthful coalesced count; a
single isolated change still arrives promptly; high-priority notifications
are not swallowed; low-priority events still participate in `digest.py`; and
old individual events remain available in the durable inbox.

### Registration checklist

**As delivered:**
1. `jarvis-cli/jarvis/clipboard_tools.py` — the module above.
2. `tools.py` — import + splice into `CORE_TOOL_SCHEMAS`, exactly where
   `AUDIO_TOOL_SCHEMAS` is spliced in today. `tool_registry.py` gets a new
   `"clipboard"` group/keywords/pack-instruction alongside it.
3. `jarvis-cli/jarvis/clipboard_watch.py` — the watch worker + its config
   helpers. `jarvis-cli/jarvis/clipboard_cli.py` — dispatch for the
   `clipboard-watch` worker subcommand and the human-only
   `clipboard-watch-config` command (split out the way `channels_cli.py`
   is). `daemons.py` — `clipboard-watch` added to `BUILTINS`. `cli.py` —
   two names added to `RESERVED_NAMES` and one new dispatch branch,
   mirroring the existing `channels_cli`/`workspace_cli` wiring.
4. No web UI changes needed for `clipboard_get`/`set`/`clear`/`wait_for_change`
   — the Debug panel already lists and can invoke any registered tool. The
   watch feature shows up in the existing Daemons panel automatically once
   started, same reasoning — it's just a fourth built-in there now.

### Testing


Unit-testable without a real clipboard by monkeypatching the platform
dispatch function (mirrors how the audio_tools.py test in this session
monkeypatched `_ps`): stub `_get_windows`/`_get_macos`/`_get_linux` and
assert `tool_clipboard_get`/`tool_clipboard_set` shape their results
correctly, and that `_LINUX_GET_CANDIDATES` falls through correctly when
earlier binaries raise `FileNotFoundError`.

What can't be unit-tested and needs a real pass on each OS before shipping:
that `Get-Clipboard -Raw` / `pbpaste` / `xclip` actually work against a real
clipboard, and that `Set-Clipboard`'s stdin trick round-trips text containing
quotes, newlines, and non-ASCII correctly.


---

# Part C — Browser control via Playwright (`browser_tools.py`)

> **CURRENT AUDIT — 2026-09-22:** Delivered in v1 + v2. The current tree includes the warm browser daemon, persistent profile and fallback path. Remaining acceptance is real Chromium execution, cross-ask login persistence, and hard-kill child cleanup behavior; these are verification/integration gaps, not absent source modules.


**Status: Delivered (v1 + v2).** `jarvis-browser-control.patch` — see the
2026-09-20i revision note above for what's in it and the two deliberate
deviations: the session lifetime is "process lifetime" (atexit) rather than a
hook in `ai_client.ask()` (registration step 5 below was replaced, not
skipped), and Playwright is imported lazily so a missing install degrades to
a clear error. Registration steps 1–4 and 6 were done as written
(`tool_safety.py` got `browser_click`/`browser_fill`; the `browser` group was
added to `tool_registry.py`). Not verified against a real browser or a real
login — see the caveat in §0.0. **The v2 warm daemon shipped in
`jarvis-partC-D2.patch` (rev. 2026-09-22a, see §0.1f)** —
`browser_daemon.py`, a `daemons.py` BUILTIN with its own idle-timeout, wired
in behind an off-by-default `browser_warm_daemon` config flag so a fresh
checkout's behavior is unchanged unless it's turned on. See "v2 warm daemon"
below, now marked done, for the full design and what it does and doesn't fix.

**For:** the implementing agent. **Effort:** medium-large — this is a real
subsystem (persistent sessions, a heavier optional dependency, safety
gating), not a short tool module like the clipboard one.

### Why this is a separate module from `web_tools.py`

`web_tools.py` is deliberately light: `urllib` + `html.parser`, no extra
dependency, no state, one anonymous GET per call, HTML stripped down to
`FETCH_MAX_CHARS` (4000) of plain text (see its docstring: *"Page fetch
strips markup and caps text so a library-sized HTML dump never hits the
model"*). That's the right tool for "what does this page say" and it should
stay exactly that fast and dependency-free.

Browser control is a different job: a **stateful, authenticated, JS-executing
session** — fill a form, click through a multi-step flow, stay logged in
between steps. Bolting that onto `web_tools.py` would force Playwright (a
~300MB-with-browser-binary dependency) onto every install just to keep
`web_fetch` working. Keep them as two modules; a tool's schema description
is where the model learns "use `web_fetch` for reading a page, `browser_*`
for interacting with one."

### Dependency and setup story

```
pip install playwright
python -m playwright install chromium   # downloads the browser binary, ~150-300MB
```

This needs a one-time setup step and a graceful story for when it hasn't
happened. Two things to build, both following existing patterns:

1. **Lazy import.** `import playwright` only inside the functions that need
   it (not at module top level), and catch `ImportError` to return
   `{"ok": false, "error": "Browser control isn't set up yet. Run: jarvis
   browser-setup"}` rather than crashing `tools.py`'s import chain for
   everyone who hasn't installed it. This mirrors how `audio_tools.py`
   guards its Windows-only code with a `sys.platform` check instead of
   assuming the environment — same idea, different guard.
2. **A setup command**, `jarvis browser-setup`, that runs the two install
   steps above and reports pass/fail per step, in the same spirit as
   `doctor.py`'s dependency checks (worth reading `doctor.py` first for its
   exact reporting shape and reusing it rather than inventing a new one).

### Session model — the decision that shapes everything else

The actual design question: **when does the browser process start and stop,
and where does login state live?**

Two things are separable and need different lifetimes:

- **Login state (cookies, local storage)** needs to survive *indefinitely*
  — across asks, across `jarvis` restarts. Playwright supports this
  natively via a persistent profile directory:
  ```python
  context = playwright.chromium.launch_persistent_context(
      user_data_dir=str(Path.home() / ".jarvis" / "browser-profile"),
      headless=True,
  )
  ```
  This is the easy part — one directory, handled by Playwright itself.

- **The running browser *process*** is the harder call. Starting Chromium
  takes ~1-2 seconds — too slow to pay on every single tool call within one
  multi-step flow, but a browser silently living forever in the background
  is exactly the kind of thing that confuses people later ("why does Jarvis
  have Chrome open").

**Recommended v1: process lifetime = one ask.** Open the persistent context
lazily on the first `browser_*` tool call in an ask's tool loop, keep it
alive across every subsequent `browser_*` call in that same ask (needs a
module-level or `ask()`-scoped handle — check how `ai_client.py`'s tool loop
threads state across sequential tool calls within one turn today, since
that's the existing mechanism to hook into rather than inventing a new one),
and close it when the ask ends (success, error, or timeout — a `finally`
around the tool-loop, not "whenever the model remembers to call
`browser_close`," since the model forgetting to close it shouldn't leak a
Chromium process). Login state persists via the on-disk profile regardless
of the process restarting next ask, so this doesn't cost the "stays logged
in" requirement — it only means a *fresh* ask pays the ~1-2s startup cost
once, which is a reasonable trade for "no lingering process to explain."

**v2 — done, rev. 2026-09-22a (`browser_daemon.py`, `jarvis-partC-D2.patch`,
see §0.1f).** An always-warm browser daemon, using the exact `daemons.py`
infrastructure the clipboard-watch plan above also leans on — a small
control process holding the browser open with an idle-timeout auto-close,
talked to over a local socket. Built as specified, with one concretization:
"local socket" is a loopback TCP socket (127.0.0.1, OS-assigned port,
written to a port file under `~/.jarvis/`) rather than a Unix domain socket,
matching this codebase's Windows-primary posture (see the module's own
docstring). The idle timeout closes the *browser*, not the daemon process
itself — the daemon keeps listening and reopens the browser lazily on the
next call, so nothing needs to notice and restart the daemon.
`browser_tools.py`'s `tool_browser_*` functions try the daemon only when
`browser_warm_daemon` (config, off by default) is on, and transparently fall
back to the v1 per-ask session on any failure — turning the flag on can't
make a call behave worse than a fresh checkout already does. Not yet
verified against a real Chromium session held open across several real
`jarvis ask` calls in a row (same caveat v1 has always carried); the socket
protocol and idle-timeout logic themselves are tested directly
(`tests/test_browser_daemon.py`, 18 checks).

### Tools to expose

Small, composable, one job each — matching `audio_tools.py`'s
volume_up/down/set/mute split rather than one parameterized mega-tool:

- **`browser_goto`** — `{"url": "..."}`. Navigates (starting the session if
  none is open yet this ask).
- **`browser_click`** — `{"description": "the Submit button"}` or a
  Playwright locator string. See "Selector strategy" below for why the
  schema should nudge toward descriptions over raw CSS.
- **`browser_fill`** — `{"description": "the email field", "text": "..."}`.
- **`browser_get_text`** — `{"description": "..."}` optional; no args reads
  the whole visible page text (truncated the same way `web_tools.py`
  truncates fetched pages — reuse `FETCH_MAX_CHARS` or a similar constant
  for consistency rather than picking a new number).
- **`browser_screenshot`** — no args. Returns a saved file path via the same
  convention `screenshot_tools.py` already uses for the desktop-screenshot
  tool (check that module for the exact save-path / present-to-user
  mechanism and match it) — this is also the model's main way to "see" page
  state it can't get from text alone (a canvas-based UI, a CAPTCHA, a
  layout-dependent form).
- **`browser_wait_for`** — `{"description": "...", "timeout_seconds": 10}`
  for pages that load content asynchronously.
- **`browser_close`** — explicit early close, for a multi-step ask that
  knows it's done with the browser before the ask itself ends.

### Selector strategy

Playwright's accessibility-first locators (`get_by_role`, `get_by_label`,
`get_by_text`) are far more robust to a site's markup changing than raw CSS
selectors or XPath — and critically, they're also what a model without a
live view of the DOM can reason about reliably from a text description
("the Submit button", "the email field"). Each tool's schema description
should explicitly say to describe *what the element is*, not
supply a CSS selector, and the implementation should try, in order:
`get_by_role` → `get_by_label` → `get_by_text` → a raw CSS fallback only if
given one directly. This is as much a prompt-design decision as a code one —
get the tool descriptions right and the model won't reach for brittle
selectors in the first place.

### Safety — this is the part that needs the most care

A tool that can click buttons and submit forms on a *real, logged-in*
session is a meaningfully bigger blast radius than anything else in this
tool set — it can buy things, send messages, delete data, change account
settings. This codebase already has the right mechanism for exactly this:
`tool_safety.py`'s `DEFAULT_CONFIRM_REQUIRED` set and `requires_confirmation(name)`
check, which is what makes a tool call pause for a person's OK via the
`JARVIS_CONFIRM_REQUEST` protocol (see `cli.py`'s confirm-request printing,
and how `server.js`/the web UI's confirm popup consumes it) instead of
running unattended.

Add `browser_click` and `browser_fill` to `DEFAULT_CONFIRM_REQUIRED` by
default. This will feel heavy-handed for "click the next-page link" — the
implementing agent should decide whether to soften it to "confirm only when
the page navigates to a new origin, or the target element's accessible name
matches a small blocklist of words (buy, purchase, pay, delete, remove,
confirm, submit, transfer)" rather than gating every click universally.
Either way, whatever the actual rule ends up being belongs in
`tool_safety.py` alongside the existing defaults, not as one-off logic
inside `browser_tools.py` — that's the established seam for this decision
in this codebase.

`browser_goto` should also refuse (or confirm) navigation to a handful of
obviously-dangerous schemes (`file://`, `javascript:`) — a page-supplied
link with a `javascript:` href passed straight to `goto` is a real footgun.

### Headless vs. headed

Default headless (`headless=True`) for normal operation. A config flag (in
whatever `~/.jarvis/config.json`-style settings file this codebase already
uses for comparable toggles — check `ai_config.py` for the existing pattern
rather than inventing a new config file) to force headed mode is worth
having for the implementing agent's own debugging, but shouldn't be
something the model can flip mid-conversation.

### Registration checklist

1. `jarvis-cli/jarvis/browser_tools.py` — the module above, with the lazy
   Playwright import.
2. `jarvis-cli/jarvis/tool_safety.py` — add `browser_click`/`browser_fill`
   (and whatever the narrowed rule ends up being) to
   `DEFAULT_CONFIRM_REQUIRED`.
3. `tools.py` — import + splice into `CORE_TOOL_SCHEMAS`.
4. `cli.py` — add the `browser-setup` subcommand.
5. Check how `ai_client.py`'s tool-execution loop passes state between
   sequential tool calls in one turn, and hook the per-ask browser-context
   lifetime into whatever that mechanism already is, with a `finally` that
   closes the context even if a tool call raises.
6. No new web UI panel needed for v1 — `browser_screenshot`'s output can
   likely reuse the existing screenshot-display plumbing
   (`screenshot_tools.py` / the extras-rendering "screenshot" case already
   in `app.js`) rather than building a new one.

### What can and can't be validated without a real environment

Headless Chromium doesn't need a physical display, so importing Playwright
and launching a headless browser *can* be smoke-tested in a plain Linux
container once the packages are installed — this doesn't require the kind
of "Windows-only, can't run it here" caveat `audio_tools.py`'s fixes needed.
The one thing that does need a real pass on an actual machine before
shipping: that the persistent profile directory actually keeps a real site's
login across two separate `jarvis ask` invocations, since that's the crux
of the whole session-model decision above.


---

# Part D — Remaining work the patch left open

> **CURRENT AUDIT — 2026-09-22:** D.1 is partial, D.2 is done, D.3 has no concrete current repro in the archive, and D.4's nullable-optional issue is already fixed. The only D-family feature still meaningfully under construction is the subagent live view.


## D.1 Subagent live view (console output + reasoning, right next to Jarvis)

**Goal (from the original request):** see what each subagent is doing —
reasoning, console output, what it's trying to do — in a separate view next to
Jarvis, and cycle through the active subagents.

**Already there (Part 0.1):** button + badge, cycling, plan/history/result,
transcript via `conv_id`.

**To do, in two tiers:**

1. **Near-live without new backend (small).** While a subagent is selected,
   poll its conversation incrementally (the same `GET /api/conversations/:id`
   the Ask panel uses, with an offset/`since` so only new entries come back)
   every ~1 s and render its saved tool calls, console output and `thinking`
   extras in the panel's detail pane. Updates arrive per completed round, not
   per token. Once Part E exists, read the subagent's console store with
   `since_seq` instead, and reuse the same filter control.
2. **Truly live, word by word (needs §8).** Each subagent step is its own
   `jarvis ask` subprocess (see `task_runner.py`). Once `JARVIS_STREAM` markers
   exist (§8.2), the runner must forward them somewhere the server can tail —
   e.g. append the marker lines to a per-task stream file/log that `server.js`
   tails and pushes over the WebSocket as `subagent-stream` events keyed by task
   id. Check how `task_runner.py` currently captures the step subprocess's
   stdout before choosing the mechanism; reuse whatever it already does rather
   than adding a second channel.
3. **Layout.** Today the panel is a full overlay. To sit "right next to Jarvis",
   make it a docked side pane in the Ask/Focus layout (collapsible, same
   prev/next cycling and status badge), keeping the overlay as the fallback on
   narrow screens. Reuse the segmented thinking → text → tool renderer from
   §8.5 so a subagent's live view looks identical to the main chat.
4. **Older tasks.** Tasks created before the patch have no `conv_id`
   (button disabled). Nothing to migrate — just show the plan/history for them.

## D.2 Notifications: show a summary, not raw output — Status: Delivered (rev. 2026-09-22a)

The reported problem: notifications listing scheduled jobs showed raw
`JARVIS_USAGE {...}` JSON under each entry ("this is wrong it should show a
summary or smth instead of the raw json").

- **Done (Part 0.1 #5):** the marker lines are stripped for scheduled asks and
  scheduled commands.
- **Done (rev. 2026-09-22a), `jarvis-partC-D2.patch`** (see §0.1f):
  - **What "summary" means:** first line / first ~200 chars of the cleaned
    reply (`ask_output.summarize()`), with the full cleaned text kept
    alongside it (`notifier.notify()`'s `message` field) for an "expand"
    view — exactly the shape suggested here. The web Notifications panel
    (`app.js`) now renders the summary with a "Show more" toggle instead of
    dumping the whole body inline; `style.css` got the small
    `.notif-card__expand` control for it.
  - **Audit of every place that creates a notification from `ask` stdout:**
    done. `notifier.notify()` call sites all pass user-authored text
    (`notify-send`, `tool_notify_me`, `ambient.py`, `clipboard_watch.py`) or
    already-scrubbed scheduler output, so they were never the leak; `notify()`
    itself now also strips defensively as a backstop for any future call site.
    `task_runner.py`'s captured step stdout **was** a second leak site beyond
    the two `scheduler.py` paths already fixed — its `text`/`body`/`summary`/
    task `result` are now stripped too, before `tasks.parse_control` ever
    sees them. "Daemon-reported output" (`daemons.py` console logs) was
    checked and found not to carry this marker — those logs aren't `jarvis
    ask` stdout. The stripping now lives in one shared helper, `ask_output.py`
    (`strip_protocol_lines`/`summarize`), imported by `notifier.py`,
    `scheduler.py` (re-exporting its old private name for compatibility) and
    `task_runner.py`, rather than being copy-pasted per call site as asked.
  - **Existing data:** handled by "strip on read" rather than a migration —
    `notifier.pending()`/`history()` now re-clean `message` and backfill a
    missing `summary` on every read, so old records self-heal the first time
    they're viewed.

> **Cross-check note (2026-09-21c), resolved:** `digest.py` already batches
> `low`-priority notifications into one scheduled, source-grouped summary
> message (`jarvis digest-on` etc.) — related to but distinct from the
> summary asked for above. The two now compose: `digest.py` groups
> notifications whose `message`/`summary` fields `ask_output.py` already
> cleaned, so a digest entry can't reintroduce raw marker JSON either.

### D.2.1 Notification importance levels — new end-to-end requirement

`digest.py` already defines the vocabulary `low`, `normal`, `high`, plus
aliases such as `urgent`/`critical` → `high` and `routine`/`quiet`/`digest`
→ `low`. That priority model is currently **not end-to-end**: the durable
record built by `notifier.notify()` has `kind` but no first-class `priority`,
and clipboard-watch currently sends a plain notification.

The new requirement is to make **importance a property of the notification
itself**, then let delivery policy interpret it consistently across inbox,
stream/toast/voice/chat delivery and digesting. Keep the existing three-level
model rather than inventing a second scale:

| Level | Default behavior | Flood/digest behavior |
|---|---|---|
| `high` | Immediate | Never batched or silently suppressed |
| `normal` | Immediate | May be coalesced during a burst, but not silently dropped |
| `low` | Routine | Eligible for digest batching; when digest is off, still receives normal delivery subject to flood coalescing |

The implementation should define **per-kind defaults** (including a deliberate
default for `clipboard_watch`), allow an explicit caller priority to override
the kind default, persist the resolved priority with each notification record,
and make the web/CLI views able to display it without changing the durable-inbox
semantics. `digest.py` should consume the same normalized priority instead of
maintaining a parallel inference path.

**Acceptance:** a notification created by each major source (`notify`,
`reminder`, `task`, `clipboard_watch`) records the expected level; explicit
priority overrides win over kind defaults; high items bypass digest/flood
suppression; low items enter the existing digest path when enabled; and the
web inbox/notification history can distinguish levels without losing the full
message.

## D.3 Items from your own scheduled reminder

A scheduled reminder in the pasted notifications reads (paraphrased): *remove
the scheduler prompt from the system prompt; make the prompts that go to the
scheduler agent get saved in logs; fix the API provider issue related to the
scheduler.* Status is **unverified** — none of it was part of the patch's
purpose:

- **Prompts saved in logs:** the scheduler already has a `_log_scheduled_ask`
  path and a `MAX_ASK_LOG_ENTRIES` cap, and the patch now logs the raw capture
  there. Confirm the *prompt* is being stored for every job type and shows up in
  Log search; if yes, this item is done.
- **Remove the scheduler prompt from the system prompt:** not done as far as
  the patch shows. Find where the scheduler guidance lives in the assembled
  prompt (`ai_client.py` prompt building) and move it behind the router /
  `search_tools` discovery so it costs no tokens on unrelated turns — same
  spirit as the token-optimization plan (`new_plan.md`).
- **"API provider issue related to the scheduler":** unspecified. Needs a
  concrete repro (which provider, what error, scheduled vs. manual run). Likely
  suspects worth checking first, both from Part A: provider/key rotation
  applying to scheduled asks the same way (§1a), and the `tool_choice:"none"` /
  null-schema 400s (§1c) that key rotation can never fix.

## D.4 Small leftovers

**CURRENT STATUS: mostly resolved.** The nullable optional / explicit `null` provider-schema issue is already covered by the current adapter/tool-schema path and `tests/test_nullable_optionals_and_withheld_tools.py`; do not reopen it as new work unless a new concrete repro appears.

- `spawn_subagent`'s `parent_id` schema (`"type": "string"`, ~line 225 of
  `subagent_tools.py`) still rejects an explicit `null` — see §1c for the fix
  and the audit of other optional-string parameters.
- Live Feed *Clear* button / Menu placement: already handled in repo 22 (Menu
  lives in the Live Feed header next to Clear). Nothing to merge.

---

# Part E — Console output: conversation persistence and output filter

> **CURRENT AUDIT — 2026-09-22:** Part E is **partial, not “nothing exists.”** The current repo has a dedicated `console_store.py`, persistent per-conversation console files, `/api/console/:id`, incremental live-run writes, clear/read CLI commands, and the basic three-group filter. The unresolved core is the Ask surface: `askTraceByConv` is still in-memory browser state and `renderAskTraceForConv()` replays only that local array, so a page reload cannot reconstruct Ask-console lines from the persistent store. The richer filter redesign and a small crash-recovery pointer cleanup also remain.


## E.0 Historical failure context — the persistence system now exists, but the Ask surface is still incomplete

Console-output persistence **was** attempted many times before this plan, and
earlier attempts failed. That history explains why E.4/E.7 are deliberately
strict. The current archive now contains a real `console_store.py` persistence
layer; the remaining problem is the incomplete Ask-console replay/filter path,
not the total absence of persistence. The original warnings remain useful as
implementation guidance:

- Do **not** add "one more save call" at the end of a turn and call it done —
  that is the shape the existing code already has (see E.3), and it is exactly
  what keeps failing in the cases people actually hit (reload, Stop, crash,
  long sessions, big output).
- Several **independent defects are stacked** (E.3). Fixing any one of them
  leaves the console still looking broken, which is a likely reason earlier
  attempts appeared to change nothing. All of them have to be closed.
- **Reproduce first, with a failing test, before changing anything** (E.6
  step 0), and treat E.7 as the definition of done — not "it saved once in my
  browser". If earlier attempts left notes, branches or diffs, read them first
  so they aren't repeated.

Two features live in this part: **(1) persistence** — console output survives
reloads, conversation switches, restarts, Stop/crash and long sessions; and
**(2) an output filter** — choose what you want to see in the console output.

## E.1 What "console output" means here — two surfaces plus a saved extra

| Surface | Where it shows | How it's fed today | Persisted today? |
|---|---|---|---|
| **Live output console** (`#console`, the Live Feed panel with the Clear button) | direct command runs (`run` websocket → `stdout` / `stderr` / `exit`) | `consoleAppend()` in `app.js` | Yes, but fragile — one `command_run` entry per run, written after the run exits (E.3 #5) |
| **Ask console / prompt trace** (the terminal-style lines in the Ask panel: `$ jarvis`, `$ tool …`, provider attempts, `$ tokens …`, errors) | every ask (`ask-stderr` and `JARVIS_MEDIA` lines) | `addAskPromptTrace()` → `askPromptLine()` | **No** — kept only in `state.askTraceByConv` in the browser's memory |
| **`console` extra** in a saved exchange | replayed by `renderThreadExtra`'s `"console"` case | `_split_console_dump()` in `ai_client.py` | Yes, but it is a **text heuristic on the model's reply**, not real console output (E.3 #2) |

## E.2 Evidence from the saved conversation `205b1493df955d1c.json`

Your instinct is right — for this conversation the console output was never
saved, and the reason is that the ask path isn't wired to save it. What the
file actually contains (15 exchanges, 23:25 → 23:50):

| Fact | Value |
|---|---|
| Exchanges answered / empty-and-interrupted | 11 / **4** (#0 `interrupted (process ended)`, #3 `no provider answered`, #9 and #10 `interrupted (process ended)`) |
| Extras saved on the 4 interrupted exchanges | **none at all** — no trace, no tool steps, no thinking, no console; `jarvis` is empty |
| `console` extras anywhere in the file | **0** |
| Tool-result / output extras anywhere in the file | **0** (the only tool calls saved are 9 one-line `trace.steps`: name + short detail, no output) |
| `thinking` extras | 2 (both on `ollama1`, #13 and #14) — none from any other provider |
| The first exchange, the one that ran the longest | saved as an empty interrupted exchange: everything it did (attempts, tool calls, output) is gone |

So the conversation file records *that* you asked things and a few tool names,
but none of what the console showed while it worked. That matches the code
below.

## E.3 Root causes found in the source

Found by reading the code against the file above. **Not yet reproduced** —
step 0 of E.6 is to confirm each one with a test.

1. **The Ask console never leaves the browser.** `askPromptLine()`
   (`app.js` ~3779) appends to `state.askTraceByConv[convId]` — client memory
   only. The server never receives those lines, so a reload, a second tab or a
   different browser loses them.
2. **The only server-saved `console` extra is a guess from the reply text.**
   `_split_console_dump(result.text)` (`ai_client.py` ~2000, used at ~2784)
   treats "everything before the first `Name:` line" and lines that look like
   echoed tool traces as a console dump. For a normal reply it produces
   nothing. It is not the real stdout/stderr/tool output.
3. **Tool output is not persisted in the exchange.** `_extras_from_runs`
   (`ai_client.py` ~1755) keeps results for only five tools (`take_screenshot`,
   `organize_json`, `ytdl_download`, `present_file`, `dev_agent`) plus
   `confirm`; `trace.steps` keeps name / short detail / ok only. Generic tools
   (`read_file`, `search_files`, shell/command tools, …) leave no saved output.
   (Raw per-call data does reach the separate debug log via `logs.log`, but
   that is the Logs viewer's store, not the conversation.)
4. **Everything is saved at the end of a turn only.** `complete_exchange()` is
   called on success (and on the "mutation already completed" path). The
   interrupted paths — `abandon_pending_turn()` → `abandon_exchange(conv_id,
   user_text, reason=reason)` (`ai_client.py` ~2219) and
   `_reclaim_stale_pending()` (`conversations.py` ~478) — save **no extras**,
   even though `abandon_exchange` already accepts an `extras` argument. Stop,
   a killed process, or all providers failing therefore lose the whole turn's
   activity (4 of 15 exchanges in the sample).
5. **The Live output console's persistence has its own defects:**
   1. Written **only when the run exits** (`runOnExit`, `server.js` ~2199). A
      server restart or crash mid-run loses that run entirely; with no valid
      conversation id it is skipped silently.
   2. The whole run is **one log entry**, and `logs._safe_json` (`logs.py`
      ~43) truncates any entry whose JSON exceeds `MAX_LINE_CHARS = 20000`:
      the cut text fails `json.loads`, so it is replaced by
      `{"truncated": true, "preview": …}`. The `lines` array disappears and the
      replay shows just the command line with **no output and no exit
      status**. Any run with more than roughly a few hundred lines is a
      candidate — verify with a test.
   3. Replay (`loadConsoleHistoryForConv`, `app.js` ~6394) fetches only the
      **last 500 entries** of the shared per-conversation log and *then*
      filters for `command_run`. Ask requests, responses, tool calls and info
      entries live in the same file, so in a busy conversation old runs fall
      out of the 500-entry window.
   4. Replay is **skipped while `state.running`**.
   5. It shares a file with the Logs viewer: **clearing logs** (`logs-clear` /
      `DELETE /api/logs/:id`) also erases console history.
   6. The Live Feed **Clear** button only empties the DOM
      (`#btn-clear-console`, `app.js` ~2421). The saved entries come back on the
      next conversation load, so "cleared" doesn't survive a reload.

## E.4 Design — persistence

**Principle: write-ahead, append-only, per line, its own store.** Console
lines are written as they happen, not assembled and saved at the end; the
store is separate from the debug log and from the final exchange record; the
final exchange is reconciled against it, never the other way round.

1. **A dedicated console store**, e.g. `~/.jarvis/console/<conv_id>.jsonl`, one
   JSON line per event:
   `{seq, ts, turn, surface: "live"|"ask", kind, stream, tool, provider, text}`.
   - `seq` is monotonic per conversation (lets the UI ask for `since_seq`);
     `turn` ties the line to an exchange (the same turn id §5/§8 should use for
     saved segments — one identity, not two).
   - **Per-line**, so there is no 20,000-char blob to truncate. Cap each
     line's `text` and cap each tool result (keep head + tail with an explicit
     "… N chars omitted" marker) — truncation is allowed but must be *visible*,
     never a silent empty result.
   - A per-conversation size cap with rotation of the **oldest** lines, and a
     visible "earlier output trimmed" marker.
2. **Writers (this is the missing wiring):**
   - **Asks:** `cli.py`/`ai_client.py` already hold the live signals — the
     `on_attempt`, `on_tool_call`, `on_tool_result` callbacks and the stderr
     trace. Append each as an event **in the same process, as it happens**:
     provider attempts and failovers, tool call + (capped) tool result, token
     line, errors, notifications. No browser round-trip, so nothing depends on a
     tab being open.
   - **Direct runs:** `server.js` appends in small batches (e.g. every ~0.5 s or
     ~20 lines) while the run is going, plus a final status line on exit — not
     one blob at exit.
   - **Subagents:** each step already runs as its own `jarvis ask`
     (`task_runner.py`) into the subagent's conversation, so they get the same
     store for free and Part D.1's live view can read it.
3. **Interrupt-safe.** Append + flush per event so a hard kill loses at most
   the last line. `abandon_pending_turn` / `_reclaim_stale_pending` must
   record "interrupted — N console lines saved" and pass real `extras`
   (trace, tool steps, thinking gathered so far) instead of an empty exchange.
4. **Replay API + UI.** `GET /api/console/:id?turn=&since=&kinds=&limit=`.
   Both consoles load from it when a conversation opens; `state.askTraceByConv`
   becomes a cache, not the source of truth. Replay must not depend on the
   500-entry log window or on `state.running` (append newly-arriving live
   lines after the replayed ones instead of skipping).
5. **Keep the `console` extra type, change its source.** Stop deriving it from
   reply text; use it as a pointer (turn id + line count) to the store. Keep
   `_split_console_dump` only as a fallback for old conversations.
6. **Back-compat.** Old `command_run` log entries stay readable by a fallback
   reader so existing history still shows; new writes go to the new store.
7. **Clear semantics** (decide and document): Live Feed **Clear** writes a
   persisted "cleared through seq N" marker so a reload matches what you see;
   a separate, explicit "delete console history" action removes lines. Clearing
   the **Logs** viewer must not touch the console store.
8. **Shared with §5 / §8.** Interim text and thinking segments are saved as
   part of the exchange (§5/§8); console events are the raw counterpart. Use one
   turn id and sequence across both so a reloaded turn can show its segments
   *and* its console, and don't invent a second persistence format for the
   same content.

## E.5 Design — console output filter

**Goal:** you choose what you want to see in the console output.

- **Display-only.** A filter never deletes or stops saving anything: hide
  everything, save a turn, show everything again — all of it is there. It works
  the same on live lines and on replayed history.
- **Needs a structured `kind` on every line**, not just a CSS class. Today the
  classes are `out / err / cmd / sys / exit-ok / exit-bad` (`consoleAppend`) and
  `cmd / tool / done / fail` (`askPromptLine`) — a starting point, not enough to
  separate the noise. Proposed kinds: `stdout`, `stderr`, `command`
  (`$ cmd`), `tool-call`, `tool-result`, `provider` (asking X key i/N,
  failover, retries), `tokens` (the `$ tokens …` line), `thinking`,
  `notification`, `status` (start / exit / stopped), `error`. The writers in
  E.4 tag lines with `kind` (and `tool`, `provider`) at write time.
- **Controls** — a filter button in both console headers, next to Clear:
  kind checkboxes with live counts; text search (substring, with regex like Log
  search so behaviour is consistent); an "errors only" quick toggle; a
  tool-name filter; a turn scope (this turn / this conversation); presets such
  as "Everything" and "Quiet (errors + tool calls)". For the Subagents view,
  the same control applies per subagent.
- **Never look like output is missing.** Show an "N lines hidden by filter"
  chip; highlight search matches; keep scroll-follow behaviour unchanged.
- **Where the preference lives:** server-side settings (like the existing
  show-reasoning-trace toggle behind `/api/think`), not `localStorage` — the
  codebase already moved favorites off `localStorage` for the same reason
  (`app.js` ~2043).
- **Performance:** switch categories with a `data-kind` attribute plus
  container classes (CSS hide/show) instead of re-rendering; trim/virtualize
  very long histories; page the replay API for large stores. Server-side
  `kinds=` filtering is for huge histories only.
- **Default:** show everything (today's behaviour) until the user changes it.

## E.6 Order of work

0. **Reproduce and pin down, with failing tests first** (because earlier
   attempts failed): a run with huge output → reload; a run killed mid-way →
   restart; a long conversation with many asks → old run gone; an ask with
   several tools → reload; Stop / kill mid-ask; Logs clear. Record what happens
   today for each of E.3's items. Use `205b1493df955d1c.json` as the reference
   for the interrupted-turn symptom (it was saved before any fix, so it will
   stay empty — the test must be a *fresh* reproduction).
1. Close the cheap, isolated defects on the existing path (E.3 #5.2–#5.6)
   so direct runs stop losing output while the new store is built.
2. Add the console store, its writer helper and the replay API.
3. Wire the ask path (attempts, tool calls + capped results, tokens, errors)
   and incremental run persistence.
4. Interrupt path: flush on abandon, real `extras`, "N lines saved" marker.
5. UI replay for both consoles; Clear semantics; `askTraceByConv` as cache.
6. Kind tagging + filter UI + preference.
7. Back-compat reader and cleanup of the heuristic `console` extra.

Build steps 2–5 together with §5/§8's turn identity (Part 0.4 steps 3–5), not
after — otherwise a second, incompatible saved-turn shape appears.

## E.7 Acceptance checklist

- [ ] A command with very large output (thousands of lines / >1 MB) shows its
      output after a reload; anything capped is marked visibly, never empty.
- [ ] A command interrupted by a server restart or kill shows its partial
      output, marked interrupted.
- [ ] After hundreds of asks in one conversation, earlier runs still replay
      (no 500-entry window loss).
- [ ] An ask that calls several tools (file read, file search, a shell command)
      shows every tool call, its capped result, provider attempts/failovers and
      errors in order after reload, in a second tab, and after a browser
      restart.
- [ ] An ask stopped with Stop, killed, or failing on every provider keeps the
      console lines up to that point, and its exchange links to them
      (regression for the 4 empty interrupted exchanges in
      `205b1493df955d1c.json`).
- [ ] Switching conversations and back, and starting a run while another
      conversation loads, neither drops nor mixes up console history.
- [ ] Clearing Logs does not remove console history; Live Feed Clear behaves as
      documented after a reload.
- [ ] Subagent conversations show their console the same way.
- [ ] Filter: every kind toggle hides/shows both live and replayed lines; the
      hidden-count is correct; text/regex search works; the preference survives
      reload; saving with everything hidden and then showing it restores all
      lines.
- [ ] Existing conversations with old `command_run` entries still replay.

---

## E.8 Implementation status (rev. 2026-09-21d)

Built in one pass, at the owner's explicit request to not split it into
phases ("no make it all in 1 call") — reproduction-tests-first (E.6 step 0)
was proposed and turned down for that reason. What follows is graded
honestly against E.3/E.4/E.5/E.6/E.7 rather than declared "done": most of
the **persistence** half is real and verified; the **filter UI** is a
first pass, not E.5 in full; two specific gaps are called out rather than
papered over.

**New/changed files** (`part_e_and_sec3.patch`, applies with `patch -p1`
against this tree — the copy that shipped alongside the doc conflicted in
two spots with work merged in separately, rev. 2026-09-22a's Part C v2 /
finish-signal merge and this tree's `RESERVED_NAMES`/forced-ending code;
both were hand-resolved the same way §0.1f's conflicts were, see the merge
note below): `jarvis-cli/jarvis/console_store.py` (new), `ai_client.py`,
`cli.py`, `web/server.js`, `web/public/{index.html,app.js,style.css}`.
New tests: `tests/test_console_store.py` (49 checks) and
`tests/test_tool_source_filter.py` (§3, 6 checks). The full existing
`tests/test_*.py` suite was re-run after these changes: all pass except
the pre-existing `test_subagents.py`/`_eligible_providers()` `None` bug
from §0.1b/§0.3, confirmed unrelated by re-running it against an
untouched copy of the tree.

**Merging against `jarvis-main_46_` (this tree, which already carries
G.1, Part C v2, D.2, §5-on-all-adapters and F.1/F.8/F.11 — see §0.1e/§0.1f
— none of which existed yet in the baseline `part_e_and_sec3.patch` was
built against).** `patch -p1 --fuzz=3` applied 16 of 18 hunks cleanly with
offsets; two needed hand resolution:
- **`cli.py`'s `RESERVED_NAMES`.** The patch's hunk collided with
  `jarvis-partC-D2.patch`'s earlier `browser-daemon` insertion at the same
  spot. Resolved by keeping both: `browser-daemon` plus the three new
  `console-append-run`/`console-read`/`console-clear` names, all in
  `RESERVED_NAMES`.
- **`ai_client.py`'s forced-ending branch.** The patch's hunk assumed the
  pre-22a shape of this code (a bare `extras=_extras_from_runs(turn_runs)`
  call); `jarvis-finish-signal.patch` had since restructured it to build
  `forced_extras` in stages and append an F.11 "pending action" extra.
  Resolved by adding the `console_pointer`/`consoleRef` append into that
  existing `forced_extras` list, after the F.11 `offered` append, rather
  than reintroducing the old single-line call.

Verified after resolution: `py_compile` clean on every changed file;
`node --check` clean on `app.js`/`server.js`; both new test files pass in
full (49 + 6 = 55 checks); the rest of the existing suite is unaffected
(the one pre-existing `test_subagents.py` failure noted above is the only
one, same as before this patch).

### Against E.3's root causes

1. **"The Ask console never leaves the browser."** Half fixed. The data
   now genuinely leaves the browser: every provider attempt, tool call,
   tool result, token count and error for an ask is written to
   `~/.jarvis/console/<conv_id>.jsonl` as it happens (`ai_client.py`,
   wrapping the existing `on_attempt`/`on_tool_call`/`on_tool_usage`/
   `on_interim_text` hooks — see `console_store.py`'s module docstring).
   **Not done:** the Ask console's own UI (`askTraceByConv` / the "»"
   trace bubbles in the ask thread) still only reads what's in this
   session's browser memory — it was not rewired to replay from the new
   store. `GET /api/console/:id?surface=ask` already returns the right
   data for this; only the UI-side wiring is missing.
2. **"The only server-saved `console` extra is a guess from the reply
   text."** Superseded, not replaced. `_split_console_dump()` and its
   `{"type": "console", "data": {"dumpLines": ...}}` extra are untouched
   and still run — removing a heuristic a real UI might still depend on
   felt like the wrong risk to take in the same patch as the fix. Every
   saved exchange now ALSO gets a `{"type": "consoleRef", "data": {"turn",
   "lines"}}` extra pointing at the real, complete record in the store.
   E.6 step 7's "cleanup of the heuristic" is therefore not done — the old
   extra is redundant, not deleted.
3. **"Tool output is not persisted in the exchange."** Fixed via the new
   mechanism, not the old one: every tool call's arguments and (capped)
   result now land in the console store regardless of which tool it is —
   `_extras_from_runs`'s five-tool allowlist was left exactly as-is. So a
   `read_file` or shell tool's output is now durable and replayable
   through `consoleRef`, just not through the older, narrower extra.
4. **"Everything is saved at the end of a turn only" / interrupted paths
   save no extras.** Fixed for `abandon_pending_turn()` (the path used by
   the web UI's Stop button, SIGINT/SIGTERM, and "every provider failed")
   — verified directly: a turn abandoned mid-tool-call now saves a real
   `consoleRef` extra with the exact line count logged before the kill,
   where before it saved nothing (`tests/test_console_store.py`'s
   `test_an_interrupted_turn_saves_what_ran_instead_of_nothing`, mirroring
   the exact 4-of-15-empty-exchanges symptom from `205b1493df955d1c.json`).
   The budget-exhausted/forced-ending path (§0.1f's F.1/F.8/F.11 work)
   gets the same `consoleRef` pointer, appended alongside F.11's pending
   proposed action — see the merge note above.
   **Known gap, disclosed rather than fixed:** `conversations.py`'s
   `_reclaim_stale_pending()` — the fallback for a process that died too
   hard for even the signal handler to run (SIGKILL, OOM, power loss) —
   was deliberately left untouched. The console data itself is **not**
   lost in that case (it was written line-by-line as it happened, same as
   always), but the reclaimed exchange record won't carry a `consoleRef`
   pointer to it, since `conversations.py` has no reference to a console
   turn id today and giving it one would mean threading a new parameter
   through `begin_exchange()` and reasoning about a circular import
   (`console_store.py` already imports `conversations.py` for
   `is_valid_id`). Worth a small follow-up; not attempted here.
5. **The Live output console's six persistence defects:**
   1. *Written only on exit* — fixed. `server.js` now flushes new lines
      to `console-append-run` roughly every 700ms while the run is still
      going, plus a final flush on exit; a killed `jarvis-web` process
      still has almost everything on disk, just possibly missing the
      final "exit …" status line from the very last flush window.
   2. *20,000-char whole-entry truncation eating `lines`* — fixed by
      construction: each console-store line is capped individually
      (`MAX_LINE_CHARS`, with a visible "chars omitted" marker), so no
      single oversized line can invalidate the JSON of anything else the
      way `logs._safe_json`'s truncate-then-reparse could.
   3. *500-entry shared-log replay window* — fixed: the console store is
      its own file with its own, much larger cap (2 MB / ~3000 lines,
      trimmed with a visible marker, not a hard-truncate) and is never
      filtered down from a shared request/response/tool_call log.
   4. *Replay skipped while `state.running`* — unchanged on purpose: still
      true, still the right call (don't repaint a panel someone is
      actively watching mid-run out from under them).
   5. *Shares a file with the Logs viewer, so clearing Logs erases
      console history* — fixed: the console store is a wholly separate
      file (`~/.jarvis/console/<id>.jsonl` vs `~/.jarvis/logs/<id>.jsonl`);
      `logs-clear`/`DELETE /api/logs/:id` cannot touch it.
   6. *Clear only empties the DOM, doesn't survive reload* — fixed: Clear
      now also calls `POST /api/console/:id/clear`, which appends a
      persisted "cleared through seq N" marker (never deletes anything);
      the Live Feed's own reload passes `afterLastClear=1` so it matches
      what the click did. Verified at the CLI level
      (`test_cli_console_append_run_and_read_and_clear_round_trip`); not
      exercised through an actual browser click (no `node_modules` in
      this sandbox — see below).

### Against E.4 (persistence design) and E.5 (filter design)

E.4's shape is implemented close to as specified: one append-only,
per-line, own-store file; `kind`/`tool`/`provider`/`turn` tags at write
time; a stateless layer for one-shot CLI processes (`console-append-run`
for `server.js`'s live runs) and an active-context layer for
`ai_client.ask()`'s single in-flight turn; capping with visible markers at
both the line and file level; a persisted, non-destructive Clear marker;
a back-compat reader for old `command_run` entries. Two deliberate
simplifications from the design as written, both explained in
`console_store.py`'s own docstring: `seq` is a wall-clock-derived,
per-process monotonic value rather than a real shared counter (there is
no single process both `server.js` and every `python -m jarvis` child
could safely lock and increment), and `turn` ids are opaque strings
minted independently by whichever side starts a turn, not a small
sequential int.

E.5 is only partly built. What exists: a segmented Output/Tools/Errors
filter plus a text search box on the Live Feed panel, backed by the real
replay API, applied identically to replayed and newly-arriving live
lines (`applyConsoleLineFilter`). What's still missing, all from E.5's own
list: per-kind checkboxes with live counts (this session shipped 3 fixed
groups, not individually toggleable kinds), a regex search mode, a
tool-name filter, a turn-scope selector, presets, and a persisted
preference (the filter resets to "All" on reload — `state.consoleKindGroup`
is in-memory only). The Ask console side of the same replay has no filter
UI at all yet, since (per point 1 above) it isn't wired into a persisted
replay in the first place.

### Against E.6's order of work

Steps 0 (reproduce-first) and 1 (cheap fixes on the old path in
isolation) were explicitly skipped at the owner's request in favor of one
combined patch; steps 2 (store + writer + replay API), 3 (ask-path
wiring + incremental run persistence) and 4 (interrupt path) are done and
covered by the new tests. Step 5 (UI replay for **both** consoles; Clear
semantics) is done for the Live Feed, not for the Ask console. Step 6
(kind tagging + filter UI + preference) is done for tagging and a first
filter UI, not for a persisted preference. Step 7 (back-compat reader) is
done; its second half (cleanup of the heuristic `console` extra) is not,
per point 2 above.

### Against E.7's acceptance checklist

Verified directly (automated tests, listed against each): large output
never silently empties, because capping is per-line and file-level
trimming leaves a marker (`test_line_text_is_capped_with_a_visible_marker`,
`test_file_size_cap_trims_oldest_with_a_visible_marker`); hundreds of asks
no longer risk a 500-entry window loss, since the store isn't windowed
that way (design-level, not literally tested with hundreds of real asks);
a stopped/killed/all-providers-failed ask keeps its console lines and
links to them (`test_an_interrupted_turn_saves_what_ran_instead_of_nothing`);
clearing Logs doesn't touch console history, by construction (separate
files, no shared code path); a Clear survives reload
(`test_cli_console_append_run_and_read_and_clear_round_trip`); old
`command_run` conversations still replay
(`test_read_legacy_command_run_adapts_old_entries`). **Not verified — no
automated test, and no live browser/network available in this sandbox to
check by hand:** the multi-tool ask actually rendering in the Ask
console's UI after a reload (backend confirmed, UI not wired — see point
1); switching conversations mid-load without drop/mix-up (the existing
stale-response guard in `loadConsoleHistoryForConv` was kept, not
newly verified); subagent conversations (they go through the same
`ai_client.ask()` code path, so this should work by construction, but
was not specifically exercised); the filter's hidden-count display and
"restores all lines" behavior (no hidden-count UI was built at all — see
E.5 above). `server.js` itself was syntax-checked (`node --check`) and
reviewed line-by-line against the original handler, but not run — this
sandbox has no installed `node_modules` and no network access to fetch
them.

**Suggested next step for whoever picks this up:** wire the Ask console
(`askTraceByConv`) to `GET /api/console/:id?surface=ask` the same way
`loadConsoleHistoryForConv` now wires the Live Feed to `surface=live` —
that closes E.3 #1 and most of E.7's still-open items in one pass, since
the backend and API already support it. After that, a real browser/`npm
install` pass to exercise `server.js`'s new incremental-flush and replay
routes live would be the highest-value verification step.

---

# Part F — Live failures of 2026-09-20: why Jarvis can't do basic tasks

> **CURRENT AUDIT — 2026-09-22:** F is mostly closed. F.1/F.3/F.4/F.5/F.7/F.8/F.9/F.10/F.11/F.12 and D5/D6 are done. F.2 is partial: the separate catalog-discovery budget exists, but project-content discovery remains in the ordinary work budget. F.6 is partial only because “last words” are not structured. F.17 is **not reproducible from the current archive** because `tests/fixtures/` is missing; the historical fixture-run result must not be treated as a current green test.


> **Source.** Two real turns from the owner's machine, each as a saved
> conversation (`.json`) plus the raw provider traffic (`.jsonl`), read against
> `jarvis-main` (repo 26). All times are UTC. Line numbers are for that copy —
> re-check if files moved. `.jsonl` references are **record numbers counted from 0**
> (file line = record + 1). **This part is analysis only; no code was changed.**
>
> - **Case 1 — `cb140e091ddc66e8`** (08:33–08:35): *"help me out… I can't get
>   the token variable from `.env` to run in the python program — use code
>   agent"*, on `D:\MyDigitalVault\prjects\ratioty\main.py`.
> - **Case 2 — `81b52bb561796954`** (09:36–09:41): *"move
>   `C:/Users/assim/Downloads/20-09-2026.pdf` to the documents/studying
>   folder"* (2a), then *"YES PLZ RUN THIS CUSTOM COMMAND PLZ"* (2b).
>
> Confidence labels used below: **Definite** = visible in the log *and*
> confirmed in the source; **Verified** = reproduced in a sandbox snippet;
> **Inferred** = consistent with the evidence, not proven.

## F.0 What actually happened

Both requests were trivial: one line of code to fix, one file to move.
**Neither got done.** A model produced the right next step at least once in
each log — the harness threw it away, or never let the model reach it.

| Case | Outcome | Wall time | Provider attempts | Useful work done |
|---|---|---|---|---|
| 1 | **No reply at all** (`no provider answered`); 4 confirm prompts | 2 min 33 s | 10 Gemini keys, 4 Groq keys, 2 Ollama (server down) | None — `edit_file` was never called |
| 2a | Told the user to run `Move-Item` themselves | 1 min 36 s | 10 Gemini keys + Groq | 2 good `search_files`; then 3 calls to tools that don't exist and 1 empty-args probe |
| 2b | Refused again, offered to "schedule" the move | 1 min 50 s | 10 Gemini keys + Groq | None — the correct call was made at 09:40:25 and **discarded** |

Across both logs 9 of 54 Gemini responses were HTTP 503, 2 were 429, and 8
were `MALFORMED_FUNCTION_CALL` reported to the user as "empty response content".

**Case 1, cause and effect.** `list_dir` hides `.env` (F.5) → the inner
`code_agent` probes for it with `dir /a`, which fails with `WinError 2`
because `run_shell` isn't a shell (F.4) → two `python -c` probes exit 0 with
*empty* stdout (F.4) → the agent's 5 rounds are gone and `edit_file` was never
reached (F.6) → `code_agent` returns `ok:false` with an opaque step list (F.6)
→ Gemini key 1 returns 429 (F.9) → key 2 restarts from a truncated recap and
repeats the same reads (F.7) → 3 more confirm prompts (F.12) → the shared
6-round budget is spent (F.2) → tools are withheld and every remaining key
fails in a different way but with the same cause (F.1, F.8) → Ollama is down →
nothing.

**Case 2, cause and effect.** The router loads only the 7 `files` tools, none
of which can move anything (F.3) → the model probes made-up tool names with
`{}` (F.2) → 4 of the 6 budget rounds are gone → 503s and empty responses
rotate keys (F.9, F.7) → Groq answers with the internal "tool budget is spent"
note leaking into the reply (F.11). Turn 2b: the user's pasted copy of that
reply drags the router onto the wrong groups (F.10) → 6 rounds of discovery
(F.2) → the 7th response is the correct `Move-Item` call → **discarded** (F.1).

## F.0b Summary of findings

| ID | Finding | Confidence | Effort | Tier |
|---|---|---|---|---|
| F.1 | A tool call made in a "tools withheld" round is discarded and the key is rotated | Definite | M | 0b |
| F.2 | Discovery and made-up-name probes burn the same budget as real work; unknown-tool errors carry no hint | Definite | S | 0a |
| F.3 | There is **no** move / copy / rename / delete-file tool at all | Definite | S–M | 0a |
| F.4 | `run_shell` isn't a shell: builtins fail; on Windows `python -c "…"` silently does nothing | Definite + Verified | S | 0a |
| F.5 | `list_dir` / `search_code` skip dotfiles, so `code_agent` can't see `.env` | Definite | S | 0a |
| F.6 | `code_agent` spends its rounds probing before it edits; its result tells the caller nothing | Definite | S | 0a |
| F.7 | Failover throws away the transcript; the recap is a truncated second "user" message | Definite | M | 0b |
| F.8 | Withheld-tools rounds fail four different ways and are all reported as "empty response" | Definite | M | 0b |
| F.9 | Quota / overload treated as key faults; no key-health memory; nested agent bursts the rate limit | Definite (mechanism) / Inferred (limit) | M | 0c |
| F.10 | Router scores the pasted previous reply; a confident wrong route replaces the sticky one | Definite | S–M | 0a |
| F.11 | Degraded answers leak internals and offer things Jarvis can't do | Definite | S | 0c |
| F.12 | Confirm churn on read-only commands; misleading diagnosis; log mislabelling | Definite | S | 0c |

Tiers are explained in F.15. **S** = under a day, **M** = a few days.

---

## F.1 A correct tool call made in a "tools withheld" round is thrown away, and the key is rotated

**Status: done — `jarvis-forced-ending.patch`** (items 1–3 of the suggested
fix). One shared "tools are gone" notice for every adapter; `KIND_BUDGET`
never rotates keys and ends through a harness-written reply; and the grace
call (item 3, owner decision D1) is implemented and **on by default** —
`defaults.grace_call = false` in `ai_config.json` disables it. The grace call
goes through the normal confirm gate. See revision 2026-09-20k.

**Evidence.**
- Case 2b, `.jsonl` lines 119→120 (09:40:25): the request carries **no**
  `tools` (14 messages of history). Gemini answers with `run_custom_command`
  and exactly the right command —
  `powershell -Command "Move-Item -Path 'C:\Users\assim\Downloads\20-09-2026.pdf' -Destination 'C:\Users\assim\Documents\studying\'"`.
  No `tool_call` event follows; the next line is `gemini (key 3/10)`. The
  saved trace says key 2 *"gave up after 5 rounds of tool calls with no final
  answer"*.
- Case 1, outer key 3, lines 83→84: request without tools → `read_file …\.env`,
  the very next step needed → discarded the same way. Case 1's inner agent,
  lines 35→36: `run_shell where python` discarded.

**Cause.**
- `call_gemini` attaches `tools` only while `round_num < MAX_TOOL_ROUNDS and
  round_budget.remaining() > 0` (`ai_providers.py:1641`). If the model still
  replies with a `functionCall` and no text, line 1707 returns
  `AIResult(False, error=_give_up_error())`. Anthropic (`:1341`) and
  openai-compatible have the same branch.
- `ask()` handles that like a dead key. `_REQUEST_SHAPE_MARKERS` (`:373`)
  doesn't list it, so the next key is tried — with the same spent budget (F.2)
  and a lossy recap (F.7).
- The "tool calls are no longer available" notice (`_TOOLS_WITHHELD_NOTICE`,
  `:943`) is attached **only** by `call_openai_compatible` (`:1037`). Gemini,
  Anthropic, Cohere and Ollama are never told their tools were removed.

**Why it matters.** This one behaviour turned "one more step" into "ten wasted
keys". It also breaks §5's rule that forced endings must be reported *as*
forced endings.

**Suggested fix** (design detail in F.8).
1. Every adapter tells the model, in plain text, that tools are gone — one
   shared notice, not one adapter's private copy.
2. "Budget exhausted" and "model wanted a tool it couldn't have" are **not key
   failures**: they must never rotate keys. Give them their own error kind and
   end the ask through a degraded reply that reports what ran and what was
   about to run (the `_completed_mutations` / `_summarize_completed_mutations`
   path already does this for finished side effects).
3. **Decision (F.16):** allow one *grace call* — if the last, tool-less
   request comes back containing a tool call, run that one call through the
   normal confirm gate, then force the text answer. It costs at most one extra
   request per ask, and it is exactly what would have moved the PDF.

**Tests.** A fake adapter that always answers with a tool call once tools are
withheld: `ask()` must not rotate keys, must return a non-empty reply, and
must include the completed tool results.

---

## F.2 Discovery and made-up-name probes burn the same budget as real work

> **CURRENT STATUS — 2026-09-22:** Partial. The separate budget for catalog-style
> discovery calls is implemented and tested. The remaining open question is
> project-content discovery (`search_files` / related repo inspection), which
> still consumes ordinary work rounds.

**Status: partially done — `jarvis-unknown-tool-hint.patch`.** Items 1 and 2
of the suggested fix below: `execute_tool`'s unknown-name path (`tools.py`)
now returns the same `did_you_mean`/`search_tools` hint `get_tool_schema`
already gave, via a shared `_unknown_tool_hint` helper — so a model that
calls a made-up name directly gets pointed at the real name or at
`search_tools` either way. The compact and ultra-compact system-prompt
blurbs (`ai_client._tools_blurb`) now say "never invent a tool name — call
search_tools first", matching what the full (non-compact) blurb already
said — those two compact variants are exactly the ones active when tool
schemas are name-only, i.e. Case 2a/2b's actual condition. **Item 3 (a
separate, uncharged discovery-call budget) is not done** — that's owner
decision D1 (changes what a "round" costs); a made-up name still spends a
full round of the same 6-round budget as a real tool call, it just gets a
useful hint back instead of a bare error. Tests:
`tests/test_unknown_tool_hint.py`, 7 cases covering both entry points,
the near-match/no-match hint split, and all three prompt-blurb variants.

**Evidence.**
- Case 2a, lines 17–35 — after two real `search_files` calls the model tried
  `move_file {}`, `run_command {}`, `run_powershell {}`, `python_eval {}`.
  Four of the six budget rounds, none of them useful.
- Case 2b — `get_tool_schema(schedule_task)`, `get_tool_schema(run_command)`,
  `get_tool_schema(powershell)`, `get_tool_schema(search_tools)`,
  `search_tools("file move command shell")`,
  `get_tool_schema(run_custom_command)` = six calls of pure discovery; the
  seventh, the real one, is the call F.1 discarded.

**Cause.**
- `RoundBudget` (`ai_providers.py:56`, `GLOBAL_MAX_TOOL_ROUNDS = 6`, shared
  across every key) is charged by `take()` for *every* function call,
  whatever it is.
- `execute_tool` answers an unknown name with a bare
  `{"error": "no such tool: X"}` (`tools.py:1064`). `get_tool_schema` on the
  same unknown name returns `did_you_mean` or *"Call search_tools with a
  keyword"* (`tools.py:425–437`) — the recovery hint exists, one code path
  doesn't use it.
- The name-only system prompt says *"If it needs arguments you don't know,
  call it with no arguments — you will get its schema"*. To a model that has
  just invented `move_file`, that reads as permission to probe.

**Suggested fix.**
1. Unknown-tool results reuse `get_tool_schema`'s hint (`did_you_mean` /
   `search_tools`).
2. Add one line to the system prompt: *if you need a capability you don't see,
   call `search_tools` first; never invent a tool name.*
3. **Decision (F.16):** don't charge zero-side-effect discovery calls
   (`search_tools`, `get_tool_schema`, `load_skill`, unknown-name errors)
   against the work budget; give them a small separate cap (e.g. 3) so they
   can't loop. `MAX_TOOL_ROUNDS` stays at 5 as `AGENTS.md` requires — this
   changes *what counts*, not the number.

**Tests.** Unknown name → result carries `hint`/`did_you_mean`; a turn of three
discovery calls plus five real calls completes; four discovery calls stop.

---

## F.3 There is no move / copy / rename / delete-file tool

**Status: done — `jarvis-path-tools.patch`.** `actions/path_tools.py` adds
`move_path`, `copy_path`, `rename_path`, `make_dir` and `delete_path` (Recycle
Bin only) to the `files` group. Differences from the suggested fix below: the
arguments are `src`/`dest` (not `dst`) so `policy.py` can see the path;
confirm-gating uses the action file's `TOOL_CONFIRM_REQUIRED`, so
**`tool_safety.py` was not changed** (owner decision D4's premise didn't hold);
drive roots, the home folder, OS folders and `~/.jarvis` are refused
outright; overwrite is opt-in and only ever replaces a file. See revision
2026-09-20j. Tests: `tests/test_path_tools.py` (106 checks, including the
router cases and the `~/.jarvis` overwrite bypass). Not verified on real
Windows or with a live model.

**Evidence.** `grep` across the whole package for `move_file`, `rename_file`,
`copy_file`, `delete_file`, `shutil.move` finds nothing (the only "move" is
`tool_move_mouse`). The `files` group (`tool_registry.py:88`) is exactly seven
tools: `search_files`, `reveal_in_explorer`, `open_file_location`, `open_file`,
`write_file`, `organize_json`, `present_file` — the seven the Case 2a trace
reports. The message "move … to the … folder" routed to `files` only because of
the word *folder* (weight 5 on `search_files`, `tool_registry.py:272`).

**Consequence.** The most ordinary file operation is only reachable through
`run_custom_command` (group `commands`, a shell string, confirm-gated), which
the router did not load. A model that has never been told it exists has to
discover it — see F.2.

**Suggested fix.** An `actions/`-style tool (see `actions/_template.py`) in the
`files` group:
- `move_path(src, dst, overwrite=false)`, `copy_path(...)`,
  `rename_path(path, new_name)`, `make_dir(path)`, and a delete that goes to
  the Recycle Bin (e.g. `send2trash`) rather than unlinking.
- Behaviour: resolve both paths, refuse to overwrite unless asked, create the
  destination folder only if told to, `shutil.move`, then **verify** the
  destination exists and return `{ok, from, to}`.
- Router keywords: `move`, `rename`, `copy … to`, `put … in`, `into the … folder`,
  with `not_with: ["mouse", "cursor"]` so it doesn't collide with mouse tools.
- **Safety:** these mutate the disk, so they must be confirm-gated. That means
  a change in `tool_safety.py`, which `AGENTS.md` forbids as a drive-by —
  submit it as its own reviewed change, not inside the router/discovery work.

**Tests.** tmp-dir tests for move/copy/rename, overwrite refusal, missing
source, missing destination folder, cross-drive move (skipped where unavailable).

---

## F.4 `run_shell` isn't a shell, and on Windows `python -c "…"` silently does nothing

**Status: fully done — `jarvis-run-shell-windows-args.patch` +
`jarvis-run-shell-diagnosis.patch` (rev. 2026-09-20o).** `_shlex_split`
now calls `CommandLineToArgvW` (via `ctypes`) on Windows instead of
`shlex(posix=False)`; a fixed `_CMD_BUILTINS` set (`dir`, `type`, `copy`,
`echo`, `cd`, …) is routed through `["cmd", "/c", command]` — the
*original* command string, not a re-joined argv, so quoting the model
wrote survives unchanged. Non-Windows now explicitly uses `posix=True`
(was already the effective behavior). Tests:
`tests/test_run_shell_windows_args.py`, 9 cases — split correctness,
builtin routing, non-builtin commands bypassing `cmd`, non-Windows never
routing through `cmd`, and a ctypes-failure fallback.

**The diagnosis-text follow-up (this section's last suggested-fix bullet)
is now done too.** `_run_shell_impl` carries a `command` field on every
outcome; `tool_diagnosis.diagnose()` special-cases `run_shell` + a
WinError-2-shaped error, naming the builtin (`"'dir' is a cmd.exe
builtin…"`) instead of the generic ffmpeg/git/tesseract advice, checked
against both the builtin and the genuine-missing-program case so one
doesn't regress into the other. Found and closed in the same patch:
`code_agent`'s own inner loop has a second `run_shell` call site
(`_execute_step`) that never went through the tool executor where the
first half of this fix lives, so its own failures — the ones that
actually surface in F.7's failover transcript and a job's recap — never
got the hint; `_run_shell_outcome()` now calls `diagnose()` directly for
the same case. `web/public/test-checklist-data.js`'s `run_shell` entry
updated per `AGENTS.md`. Tests: `tests/test_run_shell_diagnosis.py`, 14
cases, covering both call sites and the builtin/generic split. **Not
verified against a real Windows box** — no Windows, no `cmd.exe`, no
`ctypes.windll` in this sandbox; the tests patch `sys.platform` and stub
the WinAPI call to check the splitting/routing *logic* only.

**Evidence (Case 1).**
- `dir /a "D:\…\ratioty"` → `[WinError 2] The system cannot find the file
  specified`, then the same for `dir /a D:\…\ratioty`. Only `cmd /c dir /a`
  worked (record 82) — on the third attempt, each attempt behind its own
  confirm prompt and its own Groq risk-review call.
- `python -c "import os; print(os.listdir('.'))"` → **exit 0, empty stdout,
  empty stderr**, twice (lines 29, 34). A silent success is the worst failure
  a tool can return: the model believes the command ran and printed nothing.

**Cause.** `_run_shell_impl` (`actions/code_agent.py:277`) does
`shlex.split(command, posix=False)` on Windows (`:269`) and passes the list to
`subprocess.run` with no shell.
- `dir`, `type`, `copy`, `move`, `del`, `echo`, `cd` are `cmd.exe` builtins,
  not programs → `WinError 2`.
- **Verified in a sandbox:** `shlex.split('python -c "import os; print(1)"',
  posix=False)` keeps the quote characters inside the token, so Python is
  handed `-c` plus a *string literal* — a valid expression that prints
  nothing.

**Suggested fix.**
- Parse the command line the way Windows does (`CommandLineToArgvW`) instead of
  `shlex(posix=False)`; keep `posix=True` elsewhere.
- If `argv[0]` is a known `cmd` builtin, run it as `["cmd", "/c", command]`.
- **Decision (F.16):** the autonomous inner loop runs without per-step
  confirmation, so blanket `shell=True` (which would allow `&`, `|`, `>`)
  widens what it can do. Prefer the two changes above; keep argv mode.
- `tool_diagnosis.py:93` maps `WinError 2` to *"install ffmpeg/git/tesseract,
  run `jarvis doctor`"* — the wrong advice for `dir`. Add a rule: if the
  failing command's first word is a builtin, say *"`dir` is a cmd.exe builtin;
  run it as `cmd /c dir …`"*.

**Tests.** `dir`, `type`, `echo`, `python -c "print(1)"` (expect `1`), a quoted
path with spaces, `pytest -q` — all through both the standalone and inner-loop
paths, on Windows.

---

## F.5 `code_agent` can't see `.env`

**Status: done — `jarvis-list-dir-dotenv-visibility.patch`.** `list_dir`
now reports top-level dotfiles under a `hidden` key (still skipping
`.git`/`.venv`/`node_modules`/caches entirely, unwalked). `read_file` on
`.env`/`.env.*` masks values by default (`TOKEN=•••• (7 chars)`) via a new
`reveal_secrets` argument that defaults false; the autonomous inner loop's
own `read_file` branch deliberately never forwards that argument (checked
by test), so a model can't get a raw secret into a cloud provider's
transcript by asking. `search_code` can still reach a dotfile with an
explicit glob (e.g. `.env*`); an unqualified search still skips dotfiles.
Both the system prompt (`_CODE_AGENT_SYSTEM_PROMPT`) and the `read_file`/
`list_dir` tool-schema descriptions were updated to say so. Tests:
`tests/test_code_agent_dotenv_visibility.py`, 8 cases covering all of the
above plus that `.git`/`.venv` stay fully out of `hidden`.

**Evidence.** `list_dir` on `D:\MyDigitalVault\prjects\ratioty` returned
`entries: ["main.py"]`; `dir /a` on the same folder shows `.env` (12 bytes) and
`main.py` (508 bytes). The task was literally about `.env`.

**Cause.** `_list_dir_impl` (`actions/code_agent.py:162`) skips anything
starting with `.`; `_iter_source_files` (`:195`) does too, so `search_code`
can't find it either. The tool description says so, but the inner agent's
system prompt doesn't, so the agent had no way to know it should look
elsewhere — it burned rounds on shell probes (F.4).

**Suggested fix.**
- `list_dir` reports dotfiles by **name** (keep skipping `.git`, `.venv`,
  `node_modules`, caches): e.g. `hidden: [".env", ".gitignore"]`.
- **Decision (F.16) — secrets.** A cloud model that `read_file`s `.env` puts the
  token into the provider's transcript. Default `read_file` on `.env*` to
  **key names with masked values** (`TOKEN=•••• (6 chars)`); that is all the
  agent needs to write `os.getenv("TOKEN")`. A raw read stays available, behind
  an explicit argument.
- Tell the inner agent the rule in its system prompt.

**Tests.** A dir containing `.env` → `list_dir` names it; `read_file(".env")`
never returns a value; `search_code` can be pointed at dotfiles with an
explicit glob.

---

## F.6 `code_agent` spends its rounds probing before it edits, and its result tells the caller nothing

**Status: done — `jarvis-code-agent-budget-and-result-shaping.patch`.**
`_CODE_AGENT_SYSTEM_PROMPT` (a fixed string) is now
`_code_agent_system_prompt(round_limit)` (a function): the inner loop's
system prompt states its real per-attempt round limit as an actual number
and tells the model to edit as soon as the fix is evident rather than
re-reading or re-confirming, and to reserve its very last request for a
tool-less summary — all pre-existing prompt text (including F.5's
dotenv-masking guidance) is unchanged. New `_step_outcome()` /
`_run_shell_outcome()` helpers turn each completed step into a short,
tool-specific outcome line (`list_dir → 1 entry`, `read_file → 31 lines`,
`run_shell → exit 1: ModuleNotFoundError...`) recorded both on the step
event itself (a new, purely additive `outcome` field) and in a new `log`
list on `tool_code_agent`'s result — a compact "one line per call" summary
sitting alongside the existing raw `steps` telemetry, not replacing it (the
live-subagent transcript still gets the full detail).
`TOOL_RESULT_SPECS["code_agent"]` now drops `steps` outright at "low"
verbosity (mirroring `dev_agent`'s own treatment) and caps `last_error`,
since `log` is what a tight recap should read instead of the bulky raw
timeline. Tests: `tests/test_code_agent_budget_and_outcomes.py`, 51 checks
covering the prompt text, every tool's outcome line (including error paths
and a non-dict result never raising), an end-to-end run through
`tool_code_agent()`'s public interface (a fake `_run_agent_loop` drives the
real `executor`, both for a run that reaches `edit_file` within the ≤4-call
target and for one that gives up), and the `TOOL_RESULT_SPECS` shaping at
every verbosity level. **Not done:** capturing "the agent's last words" on a
failed run beyond the existing `last_error` string — every failure path in
`ai_providers.py` sets `AIResult.error` to a fixed diagnostic string, never
model-authored text, so there is currently nothing to surface; adding that
would mean changing `AIResult`/the adapter loops themselves (shared by every
caller, not scoped to `code_agent.py`), which reads more like part of Tier
0b's forced-ending redesign (F.1/F.8) than this small, independent item.
Left as a follow-up. **Not verified:** whether a real model actually
economizes its calls given the new prompt — no live model or network access
in this sandbox to check actual model behavior against, same caveat as
F.4's "not verified against a real Windows box".

**Evidence.** Round budget of the inner loop (`MAX_ROUNDS_CEILING = 5`,
`code_agent.py:61`): `list_dir`, `read_file main.py` (the fix was already
obvious after this), then `dir /a`, `python -c …`, `python -c …` → limit
reached, `edit_file` never called, result
`{"ok": false, "reason": "agent_failed", "last_error": "gemini: gave up after
5 rounds…"}`.

The result's `steps` list (11 entries in this run) is telemetry only — `start`
/`ok` markers with tool names; no outputs, no findings.
`tool_result_shaping.TOOL_RESULT_SPECS` (`:107`) covers `dev_agent` but not
`code_agent`, so it goes out unshaped. Consequence: the *outer* model, seeing
`ok:false` and a list of names, can't tell what was learned and repeats the
work itself (F.7).

**Suggested fix.**
- Give the inner agent its budget in the prompt: *"You have at most 5 tool
  calls. Read what you need in ≤2, edit by call 3, verify with call 4, and keep
  the last request for your summary."* Also: *"if you have read the file and
  the fix is evident, edit now."*
- Return one line per step with the outcome (`list_dir → 1 entry`,
  `read_file main.py → 31 lines`, `run_shell dir /a → WinError 2`), plus
  `last_error` and the agent's last words if it has any. Add a
  `TOOL_RESULT_SPECS` entry so the recap can carry it in ~600 characters.

**Tests.** Replay Case 1 (F.17) and assert `edit_file` is reached in ≤4 calls.

---

## F.7 Failover throws away the transcript; the recap is a truncated second "user" message

**Status: done — `jarvis-failover-transcript.patch` (rev. 2026-09-20n).**
The next key/provider now continues from the failed attempt's own
accumulated tool-call history instead of a truncated recap folded into a
second "user" message. Oversized results are cut in the middle so the
*end* of each survives (where an error or exit code usually lives).
`code_agent`/`run_shell`/`edit_file` count as "something real ran" for the
recap's own bookkeeping only, not for the forced-ending path's completed-
action check. The recap no longer tells the next key "you MUST call the
real tool". Tests: `tests/test_failover_transcript.py`, 18 cases.

**Evidence.** After the 429 on key 1 (line 41), keys 2 and 3 send **2
messages** — the user's text plus a recap — instead of the accumulated
transcript. Both keys then *repeat* `read_file main.py` and `run_shell`. The
recap ends with `…(truncated)` / `…(further tool results omitted)` (lines
44, 88, 117).

**Cause.**
- `ask()` rebuilds messages for every key (`ai_client.py` failover loop) and
  appends `_tool_runs_note()` (`:1910`) as a second user message, cut to
  `tool_result_budget` — **1600 characters** at 100 % Capacity (`:263`).
  `code_agent`'s own result alone exceeds that, so nothing after it survives.
- `AIResult.tool_history` (`ai_providers.py:291`) is documented as *"a list of
  generic messages … that the next provider can continue from instead of
  re-running those tools"*. Every adapter fills it in (44 references in
  `ai_providers.py`); **nothing outside `ai_providers.py` reads it** — not
  `ai_client.py`, not the tests, not the docs (grep of the whole repo). The mechanism exists and is unplugged at the failover boundary.
- The recap's boilerplate — *"No launch/install/command has run yet… you MUST
  call the real tool now (playnite_launch_action, package_install,
  run_command…)"* — is used whenever no *mutating* tool ran. `code_agent`,
  `run_shell` and `edit_file` aren't in `_is_mutating_tool` (`:1880`), so after
  a failed coding job the model was told to call *something*, not to report.

**Suggested fix.**
1. Feed `result.tool_history` to the next attempt (it is already in the
   generic format). Truncate the *middle* of oversized results, never drop the
   whole tail.
2. Mark `code_agent`, `run_shell`, `edit_file` as side-effecting in the recap
   (this is the recap's classification only — it is not `tool_safety.py`).
3. Replace the launch-specific nudge with neutral wording when the task isn't a
   launch/install.

**Tests.** Failover with a fake adapter whose first attempt made N tool calls:
the second attempt's request contains all N results and makes no repeat call.

---

## F.8 Withheld-tools rounds fail four different ways, all reported as "empty response"

**Status: mostly done — `jarvis-forced-ending.patch`.** Done: item 1
(flattened final request — every adapter re-enters itself with flat text), item
4 (harness-written reply instead of another key), item 5 (`repo_browser.` /
`functions.` prefixes stripped), and the recovery of a call the model wrote
into Groq's `reasoning` channel or as plain text. Item 3 is **half done**: the
central classifier exists (`classify_failure`, `KIND_*`, `AIResult.kind`) but
only `KIND_BUDGET` and `KIND_SHAPE` change rotation; "a `network` failure skips
its sibling providers" is not built. Item 2 (Gemini `mode = "NONE"`) was
replaced by the flatten design and is no longer needed. Tested with canned
responses; the recorded log bodies (F.17) were not replayed. **Rev.
2026-09-22a (`jarvis-finish-signal.patch`, see §0.1f):** the harness-written
reply this section's item 4 produces is now reported through
`AskResult.ending`/`TurnTrace.ending` ("forced"/"cutoff") instead of the old
blanket "every provider failed" sentence; item 3's still-missing
network-sibling-skip is unaffected and remains not built.

The same root condition — *budget spent, model still wants a tool* — surfaced
as:

| Provider | What the log shows | What Jarvis reported |
|---|---|---|
| Gemini (6 keys in Case 1, 2 in Case 2) | `finishReason: MALFORMED_FUNCTION_CALL`, empty text, 300–600 thought tokens | `empty response content` (`grep MALFORMED` finds nothing in the code) |
| Groq keys 1–3 (Case 1) | `content: ""`, the tool call written **as JSON inside `reasoning`**, `finish_reason: stop` | `empty response content` |
| Groq key 4 (Case 1) | HTTP 400 `tool_use_failed`; `failed_generation` names `repo_browser.run_shell` (gpt-oss's own prefix) | request-shape error → the §1c retry re-attached tools → the model called `list_dir` → `take()` refused (budget) → `_give_up_error` |
| Ollama ×2 | connection refused on `localhost:11434` (server not running; both providers share that host) | two failures, the second guaranteed |

Each attempt cost 1–8 s, and none could succeed. The §1c retry is worthless
when tools were withheld because the *budget* is spent: any tool call it
provokes can't be executed.

**Suggested design — a "final answer" request with a different shape.**
1. When tools must be withheld, build the request from a **flattened** history:
   tool calls and results become one plain-text block ("What was done: … /
   Results: …") instead of `functionCall` / `tool` messages the model can
   imitate. No structure to imitate ⇒ no malformed calls, no
   `tool_choice: none` 400s, no reasoning-channel tool calls.
2. Use the provider's native "no tools" switch where one exists (for Gemini,
   `toolConfig.functionCallingConfig.mode = "NONE"` with the declarations left
   in place — **verify against current docs before relying on it**).
3. One central classifier: `wants_tool_but_withheld`, `key`, `shape`, `network`,
   `empty`. Rotation policy follows the kind (F.1): only `key` and `network`
   rotate; a `network` failure on a `base_url` skips its sibling providers.
4. If the final answer is still empty or contains a call → harness-written
   degraded reply (F.11), not another key.
5. Strip a leading `repo_browser.` from tool names before lookup (cheap,
   harmless, gpt-oss-specific).

**Tests.** Recorded MALFORMED / reasoning-only / 400 bodies from these logs
drive an adapter test; each must end in a non-empty reply without rotating.

---

## F.9 Quota and overload are treated as key faults; nothing remembers which keys are bad

**Status: done — `jarvis-key-health.patch` (rev. 2026-09-20n) plus D5
(2026-09-21, this patch).**
New `key_health.py` (`~/.jarvis/key_health.json`, hashed key ids only): a
429 parks a key for the delay the provider stated (surfaced as `[retry in
Ns]`), a 503 demotes the whole provider ~45 s and skips its remaining keys
this attempt, a refused connection skips sibling providers on the same host.
Cooling keys are reordered to the back, never removed; every ask starts on
the last key that worked. `code_agent`'s inner loop starts on a different
key (`spread_keys()`) and reports the keys it burns so the outer ask avoids
them too. `doctor` flags an unreachable Ollama and reports cooling keys.
**D5** (wait ≤30 s on a 429's own stated retry delay before rotating,
instead of rotating at once) is done: `_short_429_wait_seconds()` in
`ai_client.py`, capped by `defaults.max_429_wait_seconds` (default 30, 0
disables), retries the same key once. Found already coded (unclear by
whom) when this patch was built, but its own regression test
(`tests/test_short_429_wait.py`, 24 cases) never re-ran the *existing*
suite: `tests/test_key_health.py`'s two-asks test used a 26 s stated
delay — inside D5's cap — so it started silently retrying in place, its
`["k1","k2"]` assertion went stale, and since that test's harness doesn't
mock `time.sleep` the way D5's own test does, the suite was actually
sleeping 26 real seconds per run. Fixed by bumping that fixture's delay
past the cap (45 s) so it goes back to testing what its name says —
cross-ask cooldown bookkeeping — without invoking D5's retry path. Tests:
`tests/test_key_health.py`, 36 cases (was 35); `tests/test_short_429_wait.py`,
24 cases. **Not done:** true per-round pacing of the inner loop (distinct
from just isolating its key) would touch the shared adapter round loop in
`ai_providers.py` across all five adapters — out of scope for this item,
same reasoning as F.6's "last words"; suggested-fix item 3 below says "pace
… or give it a different key", and the "or" is satisfied by `spread_keys()`.

**Evidence.**
- Case 1, line 41: the 429 says *"generate_content_free_tier_requests, limit: 5,
  model: gemini-3.6-flash … retry in 26.06 s"*. Key 1 had served **7 requests in
  22 s** — 1 outer and 6 from `code_agent`'s inner loop, which uses the **same
  key**. (Definite for the message; that the ten keys are all free-tier is
  **Inferred** from this one body.)
- 503 *"model is currently experiencing high demand"*: 9 of 54 Gemini
  responses. That is a model-wide condition; another key on the same model a
  few seconds later has little better odds, and each hop costs the turn its
  state (F.7).
- Case 2b started on `gemini (key 1/10)` again — the key that had returned a
  503 in 2a — and got another 503 on its second request.
  `ai_config.provider_keys()` returns keys in fixed order and
  no per-key state exists (`grep` for cooldown/health finds only `ambient.py`).

**Suggested fix.**
1. `~/.jarvis/key_health.json` (state must be on disk: every `jarvis` call is
   a new process — `AGENTS.md`): per key `{cooldown_until, last_status,
   last_ok}`. A 429 sets `cooldown_until` from the body's retry delay; a 503
   sets a short **per-model** cooldown; start each ask at the last key that
   worked.
2. Prefer *waiting* ≤ ~30 s on a 429 with a stated retry delay to abandoning a
   half-finished turn (**decision, F.16**).
3. Nested agents multiply request rate. Pace the inner loop, or give it a
   different key (`subagents.py` already isolates keys for child agents).
4. `doctor` should flag an unreachable Ollama and skip a provider whose
   `base_url` just refused a connection.

**Tests.** Two consecutive asks: the second skips the key that returned 429;
a `retryDelay` past its deadline re-admits it.

---

## F.10 The router scores the pasted previous reply, and a confident wrong route replaces the sticky one

**Status: fully done — `jarvis-router-paste-confirmation.patch` +
`jarvis-router-last-line-highlight.patch` (rev. 2026-09-20n).** Items 1 and
2 of the suggested fix below landed first. New
`conversations.last_assistant_reply` gives `ai_client._strip_pasted_previous_reply`
what it needs to strip an exact, substantial (20+ char) match of Jarvis's
own previous reply out of the text handed to `tool_router.route()` —
confirmed against the real Case 2b text that this alone flips the route
from `channels`/`scheduling` (off the assistant's own "let me know") to
`commands` (off the user's own "run this custom command"). Separately,
`_looks_like_short_confirmation` + `_merged_sticky_groups_for_confirmation`
make a confident-but-narrow route on a short confirmation ("yes plz run
this...") merge into a still-live sticky group instead of replacing it —
wired in by mutating the `RouteResult` in place so every downstream reader
in `ask()` (`active_schemas`, and `_build_messages`'s pack-instructions/
offered-names) sees the same merged view, while `turn_trace` still shows
the raw pre-merge decision since it snapshots at `note_route()` time.
`route_stickiness.py`'s docstrings were updated to stop asserting an
unconditional "never merges" now that `ask()` deliberately does, in this
one case.

**Item 3 and the highlight-quote path are now done too.** The last line of
a multi-paragraph message (3+ lines, 200+ characters) gets a small routing
bonus (+2) — the router-scoring change item 3 originally deferred, done with
`tests/interactive_inspector.py`'s mirror updated in the same change per
`AGENTS.md`, `--examples` showing no drift. A highlighted-excerpt prompt
(`web/server.js`'s path, previously untouched) is now stripped back to the
user's own words before routing, in Python, mirroring the JS wording rather
than changing it — **a real coupling to watch**: if the JS wording ever
changes, this strip silently stops matching and the bug returns with
nothing to catch it, since the test can only exercise the Python side.
Tests: `tests/test_router_last_line_and_highlight.py`, 11 cases, including a
"before" case that reproduces the original bug against the unstripped text.

**Drift guard (rev. 2026-09-20q).** `tests/test_highlight_wrapper_drift.py`
now reads the wrapper's literals out of `web/server.js` and checks the Python
mirror against them, closing the "no test can see the JS side" gap described
above; comment-only edits in `ai_client.py` and `web/server.js` point each
side at the other.

**Not done — as of the first pass only; every item below was closed by later
patches (rev. n) and is kept for history:**
- **Item 3** (give the last line of a multi-paragraph message extra
  weight) — untouched. It means editing `tool_router.route()`'s actual
  scoring, and `AGENTS.md` requires a matching update to
  `tests/interactive_inspector.py`'s mirrored copy in the same change so
  it doesn't start reporting `MIRROR DRIFT`. Skipped since items 1–2
  already resolve the observed failure; a real router-scoring change
  deserves its own focused pass rather than riding along here.
- **The highlight-quote path** (`web/server.js:2264`, this section's own
  cause bullet 3) is untouched — this patch only covers the raw-paste
  case that Case 2b actually was. A highlighted excerpt still gets folded
  into the same prompt string server-side and would still score the same
  way; fixing that means either passing text/excerpt separately through
  to the router or wrapping the excerpt in a marker the router strips,
  and touches JS, not this Python patch.
- **The new test file was compiled but not run** as part of this change —
  `python3 tests/test_router_paste_and_confirmation.py`.
- **Found and fixed by running it (owner-reported):** the first cut of
  `_looks_like_short_confirmation` matched only a leading prefix, so
  "please install ffmpeg for me" false-positived as a confirmation
  (`"please"` is a genuine confirmation word AND an ordinary sentence
  opener). Fixed by anchoring the regex across the whole message instead
  of just its start — see the 2026-09-20f revision note up top. Still not
  run by me after the fix, only compiled; re-run to confirm.

**Evidence (Case 2b).** The message is Jarvis's own previous reply pasted
verbatim (no `The user highlighted this excerpt…` wrapper — so this was a
paste, not the UI's highlight-quote), then the user's line: *"YES PLZ RUN THIS
CUSTOM COMMAND PLZ"*. The trace shows matches for `commands` (`command`),
`files` (`folder`), `channels` (`let me know`), `scheduling` (`let me know`).
Groups loaded: **`channels` + `scheduling`**. The reply's closing line
*"Let me know if you'd like me to handle it…"* out-scored the user's own
instruction.

**Cause.**
- `route(user_text)` scores the whole message; `"let me know"` is worth 9 in
  both `notify_owner` (`actions/notify_owner.py:115`) and `notify_me`
  (`actions/scheduler_tools.py:710`) versus 6 for `command`.
  `ROUTER_MAX_GROUPS = 2` (`tool_router.py:44`) keeps only the top two.
- Stickiness (`ai_client.py:2375–2407`) only applies when the route is **not**
  confident. A confident mis-route *replaces* the previous turn's `files`
  group — the very thing the follow-up needed.
- The server's highlight-quote path (`web/server.js:2264`) folds the excerpt
  into the same prompt string, so it would score the same way.

**Suggested fix.**
1. Route on the user's own words. For the highlight path, pass text and excerpt
   separately (or wrap the excerpt in a marker the router strips). For pastes,
   remove any run of text that matches the previous assistant reply in this
   conversation — the reply is on disk already.
2. A short confirmation ("yes", "do it", "run this", "go ahead") keeps the
   previous turn's groups **and** adds any group its own words match, instead
   of replacing them.
3. Give the last line of a multi-paragraph message extra weight.

**Tests.** Pasted reply + "run this custom command" → `commands` loaded and
`channels`/`scheduling` not; "yes plz" after a `files` turn → `files` kept.

---

## F.11 Degraded answers leak internals and offer things Jarvis can't do

**Status: done, including the pending action — `jarvis-forced-ending.patch`
+ `jarvis-finish-signal.patch` (rev. 2026-09-22a, see §0.1f).**
`_forced_ending_reply` builds the reply from what actually ran and the call
that was pending (with the command text), never uses the words
tool/budget/exhausted/round, and never asks the model to explain limits. The
"later idea, larger" is now built too: the offered call is saved as a
`pendingAction` extra on the exchange, and a bare *yes* ("go ahead", etc.,
via the same `_looks_like_short_confirmation` F.10 uses) on the very next
message runs it directly — through the normal `tool_executor`, so
`tool_safety.py`'s confirm gate still asks first. Only the last exchange's
action counts, it expires after 30 minutes, the tool must still exist in the
session's schemas, and it never fires for a chat guest.

**Evidence.** Both Groq replies say *"Since I've exhausted my tool calls for
this turn…"* — the harness's own notice, repeated to the user. Turn 2b then
offers *"I can schedule the operation to run automatically… once you give the
green light I'll set up a `run_command` task"*: a capability the model
invented to avoid saying "I failed", and `run_command` is the saved-command
runner, not a scheduler.

**Suggested fix (done — see Status above).** When a turn ends by force
(F.1/F.8), the **harness** writes the reply from facts: what ran and its
result, what could not be completed, and the exact next action *in the
user's terms* — with the command ready to confirm ("say 'go ahead' and I'll
run `move_path: a -> b`"). The model is never asked to explain the budget.
The "pending proposed action" per conversation, so a bare *yes* after a
proposal executes it directly, is built.

**Tests.** Forced ending → reply contains no "tool budget", "exhausted", or
"tool calls"; contains the pending command text. `tests/test_finish_signal.py`
adds, end to end through a real `ai_client.ask()` with the round budget
shrunk to force a real ending: the harness reply, the saved `pendingAction`
extra, and a follow-up "go ahead" running it with **zero** provider calls
made — plus a non-confirmation reply, a chat guest's "go ahead", and a
since-removed tool name each correctly ignored, and a `write_file` pending
action still asking for confirmation and doing nothing when declined.

---

## F.12 Confirm churn, misleading diagnosis, log mislabelling

**Status: done.** *Log mislabelling* — done, `jarvis-log-labels.patch`
(rev. 2026-09-20n). *Misleading diagnosis* — done for the two cases that
surfaced (F.4's cmd builtins, rev. o; and OCR failures, rev. p, which had the
same shape of bug: a generic matcher reading words out of a long install
note). *Confirm churn* — done (2026-09-21, D6): a fixed, exact allow-list
(`tool_safety.is_allowlisted_read_only_shell` — `dir`/`type`/`where`/`echo`,
first token only, no shell metacharacters anywhere in the command) skips
confirm **and** ai_review for `run_shell` calls that match it exactly;
wired as its own top-of-block check in `ai_client._make_tool_executor`,
isolated from the general confirm-gate condition, per `AGENTS.md`'s
protected-confirm-gate rule. Tests: `tests/test_readonly_shell_confirm.py`,
29 cases, including one confirming the bypass doesn't leak into other
confirm-gated tools. *Unexplained latency* — nothing to build; `jarvis
doctor` remains the first check.

- **Confirm churn.** Case 1 asked for confirmation four times; three of them
  were the same read-only `dir` (each also a Groq risk-review call; one
  confirm took 32 s of the user's time, 08:34:17→08:34:49). A tiny allow-list
  of read-only commands (`dir`, `type`, `where`, `echo`) could skip the prompt,
  but confirm gating is `AGENTS.md`-protected — **its own reviewed change**,
  never bundled with anything in this part.
- **Log mislabelling.** The Groq risk-review requests are logged under the
  active attempt's label (`gemini (key 2/10)` with a `api.groq.com` URL, e.g.
  lines 5–8, 52–55), which makes the Logs viewer misreport which key spent what.
- **Unexplained latency (Inferred).** The first `search_files` in Case 2a took
  28 s (09:36:47 → 09:37:14). Everything may have been cold or unavailable;
  `jarvis doctor` is the first check.

---

## F.13 Corrections and side observations

- **§1f (typo).** §1f treated `D:\MyDigitalVault\prjects\…` as a user typo the
  model should have questioned. Both new logs show `prjects` is the **real
  folder name** — `list_dir` and `dir` succeed on it, a saved memory points at
  `…\prjects\whole jarvis\jarvis v2\jarvis`, and the saved command
  `jarvis tts_bot` starts in `…\prjects\tts bo`. The original failure was a wrong *nested* path, not a
  typo. Don't build "path sanity" logic on that premise.
- **Streaming (§8)** wouldn't have fixed these failures, but it would have made
  them visible: the user waited 1½–2½ minutes with no output either time.
- **Case 1's `.env` is 12 bytes.** A Discord bot token is ~70 characters, so the
  value in that file is probably a placeholder or truncated — worth checking
  once the code is fixed. (Inferred; the value is never read.)

---

## F.14 What a correct run looks like (the budget has no slack)

**Case 2 — move the PDF:** `search_files` (or use the path given) →
`move_path` (confirm) → one-line reply. **2 tool calls, 3 requests.** Today:
6 rounds, 10 keys, no move.

**Case 1 — fix the token:** `list_dir` (sees `.env`) → `read_file main.py` →
`read_file .env` (keys only) → `edit_file` (`token = os.getenv("…")`) →
`run_shell` (`python -m py_compile main.py`) → summary. **5 tool calls plus the
tool-less summary request — the entire budget.** One wasted probe (as happened)
and the run fails; this is why F.2, F.4, F.5 and F.6 come before anything
subtle.

**For the record, `main.py` (31 lines, read in Case 1) needs:**
`token = os.getenv("<NAME_IN_.env>")` — `load_dotenv()` only fills
`os.environ`, and `print(token)` at line 31 raises `NameError` because `token`
is never assigned. The bare `os.environ` on line 7 does nothing. Separately:
`onready` should be `on_ready` (it already has `@client.event`); `on_message`
needs `@client.event`; the two `print`s and the `"{prefix}ratio"` check are
missing their `f` prefix; and `client.run(token)` is never called.

---

## F.15 Order of work

**Tier 0a — independent, small, high payoff (do first):**
1. ~~F.4 `run_shell` (builtins + Windows argument parsing + diagnosis text).~~
   **Done** — `jarvis-run-shell-windows-args.patch` (diagnosis text not
   included; see F.4's own status note).
2. ~~F.5 dotfiles visible, `.env` values masked.~~ **Done** —
   `jarvis-list-dir-dotenv-visibility.patch`.
3. ~~F.2 unknown-tool hint + system-prompt line~~ **Done (items 1–2)** —
   `jarvis-unknown-tool-hint.patch`. The discovery-budget half of this item
   is still open, pending owner decision D1.
4. ~~F.6 inner-agent prompt and result shaping.~~ **Done** —
   `jarvis-code-agent-budget-and-result-shaping.patch`. "The agent's last
   words" half of this item is still open (see F.6's own status note).
5. ~~F.10 router: route on the user's own words; sticky-merge for
   confirmations.~~ **Done (items 1–2)** —
   `jarvis-router-paste-confirmation.patch`. Item 3 (last-line weight) and
   the highlight-quote (`web/server.js`) path are still open — see F.10's
   own status note.
6. ~~F.3 move/copy/rename tool~~ **Done** — `jarvis-path-tools.patch`; it turned out not to need a `tool_safety.py` change (see F.3).

**Tier 0b — core loop; do together with §5/§6 (Part 0.4 step 3), not after:**
7. ~~F.1 + F.8 forced-ending design~~ **Done (in part)** — `jarvis-forced-ending.patch`
   (flattened final request, shared notice, error kinds, no rotation on budget,
   grace call). Sibling-provider skipping on network errors is still open.
8. ~~F.7 plug `tool_history` into failover; fix the recap.~~ **Done** — `jarvis-failover-transcript.patch`.

**Tier 0c — after 0a/0b:**
9. ~~F.9 key health and pacing.~~ **Done (in part)** — `jarvis-key-health.patch`: key/model cooldowns on disk, last-good key first, 503 and refused-connection skipping (F.8 item 3), inner agent on a different key, doctor checks. Not done: waiting on a stated 429 delay (D5), inner-loop pacing.
10. F.11 harness-written degraded replies (**done**, see F.11); F.12 polish: log mislabelling **done** (`jarvis-log-labels.patch`); confirm churn still open (protected).

**Also done:** F.10 item 3 and the highlight-quote path — `jarvis-router-last-line-highlight.patch`.
**Also done since:** F.4 diagnosis text (`jarvis-run-shell-diagnosis.patch`), §2's OCR diagnosis (`jarvis-ocr-diagnosis-fix.patch`), and the F.10 highlight-wording drift guard (rev. q).
**Still open:** F.6 last words, F.2 free discovery calls for project-content
tools like `search_files` (a narrower question than D1, which is otherwise
done), F.11 pending action, F.17's Case 2b (known gap, tied to the above).
**Resolved 2026-09-21:** D5, D6, D7, F.17 fixtures (built and run).

Build the replay fixtures (F.17) **before** 0b so the change is measured
against the real failures.

## F.16 Decisions needed from the owner

| # | Decision | Why it can't be made silently |
|---|---|---|
| D1 | Grace call, and/or free discovery calls (F.1, F.2) | Changes what a "round" is; `AGENTS.md` says `MAX_TOOL_ROUNDS` stays 5 and must never be lowered — this raises effective work, not the constant |
| D2 | Mask `.env` values on read (F.5) | Changes what the agent may see; costs the ability to debug a wrong value |
| D3 | argv mode + builtin wrapper, not `shell=True` (F.4) | Security posture of an unattended loop |
| D4 | New file-mutating tools + safety classification (F.3) | `tool_safety.py` is protected; needs its own review |
| D5 | Wait ≤30 s on a stated 429 retry delay vs rotate at once (F.9) | Latency vs preserving turn state — **done 2026-09-21** |
| D6 | Read-only allow-list for `run_shell` confirms (F.12) | Confirm gating is protected — **done 2026-09-21** |
| D7 | Should `clipboard-watch`'s pattern be settable by the model? (Part B) | Changes what an unattended daemon watches for, no confirm prompt in the loop — **done 2026-09-21** |

**Outcomes so far.** **D1:** the grace-call half was implemented and is on by
default (`defaults.grace_call`); the free-discovery-calls half — a separate,
uncharged round pool for pure catalog-lookup probes (`search_tools`,
`get_tool_schema`, `load_skill`, and made-up names) — turned out to
*already be implemented* too (`RoundBudget.discovery_limit`, wired to
`defaults.discovery_call_budget`, default 3, on by default;
`tests/test_discovery_round_budget.py`, 41 cases) — found already shipped
when this line was checked, owner unclear. **Correction:** this does
*not* cover F.17's Case 2b — that turn burns its rounds on `search_files`
(project-content discovery), which was deliberately left out of
`DISCOVERY_TOOL_NAMES` (it does real, if read-only, filesystem work,
unlike a catalog lookup) — see F.18's still-`[ ]` Case 2b line. Whether to
extend the discovery pool to cover `search_files`/`list_dir` too is a new,
narrower open question, not yet decided. **D4:** resolved without touching
`tool_safety.py` — the action-file
`TOOL_CONFIRM_REQUIRED` mechanism was enough. **D2 and D3:** implemented as
proposed — F.5 masks `.env` values on read by default (raw read only behind
an explicit argument the inner loop never forwards) and F.4 keeps argv mode
with a `cmd /c` wrapper for builtins rather than `shell=True`; both are in
the merged tree. If you disagree with either, they're small to reverse.
**D5** (2026-09-21): done — see F.9. **D6** (2026-09-21): done — see F.12.
**D7** (2026-09-21): done — `clipboard_tools.tool_clipboard_watch_set_pattern`
/ `tool_clipboard_watch_get_pattern`, no confirm gate (matches how
start/stop already work for this daemon); `clipboard_watch.set_pattern()`
does the actual validation (`re.compile`) and persistence, unchanged. The
getter wasn't explicitly asked for but was added alongside it — a setter
with no way to see the current pattern first works blind, and it's
read-only. Tests folded into `tests/test_clipboard_watch.py` (8 new cases,
22 total in that file).

## F.17 Use the two logs as regression fixtures

**HISTORICAL STATUS: previously marked done in an earlier archive. CURRENT ARCHIVE STATUS: NOT REPRODUCIBLE — `tests/fixtures/` is absent, so the two replay fixtures cannot run today. Restore them before marking F.17 current-green.** `tests/test_replay_fixtures.py` (built this
patch): the `.jsonl` bodies are bucketed into per-URL FIFO queues (more
forgiving than strict global-order replay — the fixed code is allowed to
make a different number of calls to one provider than the original run
did, as long as it asks the same endpoints in the same relative order) and
`ai_providers._post_json` is stubbed to serve them; the provider config
needed to reach the original key counts is reconstructed from the traffic
itself (real keys obviously aren't logged) and pinned via
`defaults.provider_priority`; confirm-gated tools are auto-approved,
matching what the real conversations' own `extras` show happened.

**Results, honestly:**
- **Fixture 1** (`cb140e091ddc66e8`) **passes**: reaches `code_agent` with
  the correct task and root, ends with a real non-empty reply instead of
  `no provider answered`. One structural limit, not a bug: this replay is
  offline/cross-platform (Linux, for CI), and the original ran on the
  owner's Windows box against real files — the on-disk edit itself can't
  be verified here, only the routing (right tool, right args).
- **Fixture 2b** (`81b52bb561796954`) **is a known, tracked gap, not a
  pass**: the replay still burns its rounds on two `search_files` calls
  and ends by telling the user to run the move themselves, instead of
  calling `move_path`. This is the exact F.2/D1 discovery-budget gap
  above (project-content search isn't in the free discovery pool) — the
  test records this as a separate "known gap" bucket (not a hard FAIL),
  so the suite stays green while this stays visibly unresolved; it should
  flip to a real pass automatically once that's decided, no test edit
  needed.

Fixture files live under `tests/fixtures/` as originally specced (not
committed to this doc/patch — see F.17's own redaction note; they were
provided directly for this run).

## F.18 Acceptance checklist

- [~] `dir /a`, `type file`, `echo hi` and `python -c "print(1)"` return real
      output through `run_shell` on Windows; an unknown command's error names
      the actual problem. **Logic implemented and unit-tested (sys.platform
      patched, WinAPI call stubbed) — not run on a real Windows box, so
      leaving this unchecked until someone confirms there.** The "unknown
      command's error names the actual problem" half is done too (rev. o).
- [x] `list_dir` on a folder containing `.env` names it; no tool returns a
      `.env` value by default. Verified by
      `tests/test_code_agent_dotenv_visibility.py`.
- [x] Calling an unknown tool returns `did_you_mean` / a `search_tools` hint.
      Verified by `tests/test_unknown_tool_hint.py`. (The system prompt also
      now says not to invent a name; the separate discovery-budget cap from
      F.2's item 3 is done and on by default (D1) for catalog lookups —
      `search_files`/project-content discovery is a separate, still-open
      question, see F.16's D1 note and F.17's Case 2b result below.)
- [~] "Move X to Y" completes with one confirm prompt and verifies the result. **Tool, confirm-gating, verification and routing implemented and tested (`tests/test_path_tools.py`); not yet seen end-to-end with a live model.**
- [x] Replay of Case 1 ends with an edit, not `no provider answered`.
      Verified by `tests/test_replay_fixtures.py` — see F.17. (Can't verify
      the on-disk edit itself in this sandbox; verifies routing only, see
      F.17's own note.)
- [ ] Replay of Case 2b executes the `Move-Item` call it previously
      discarded. **Still `[ ]`, correctly** — `tests/test_replay_fixtures.py`
      replays this for real now (F.17) and it still doesn't happen; recorded
      there as a known gap tied to D1's still-open `search_files` question,
      not silently marked done.
- [x] A turn that runs out of rounds returns a useful reply listing what ran and
      what remains, **without** rotating to another key and without mentioning
      "tool budget". Verified by `tests/test_forced_ending.py` (canned
      responses, not the real logs).
- [x] Failover carries the previous attempt's tool results; no repeated
      `read_file` after a rotation. Verified by `tests/test_failover_transcript.py` (fake adapter, not the real logs).
- [x] After a 429, the next `jarvis` process does not start on the same key
      until its cooldown passes. Verified by `tests/test_key_health.py`.
- [x] A pasted copy of Jarvis's previous reply doesn't change which tool groups
      load; "yes" after a `files` turn keeps `files`; a highlighted excerpt
      doesn't vote in the router. Verified by
      `tests/test_router_paste_and_confirmation.py` and
      `tests/test_router_last_line_and_highlight.py` (run on the merged tree,
      rev. n); the JS↔Python wording coupling is guarded by
      `tests/test_highlight_wrapper_drift.py` (rev. q).
- [x] No provider ever sees a tool-less request without the shared notice. (All five adapters route through `_forced_ending`; tested with canned responses.)
- [x] After a forced ending offers one call, a bare "go ahead" on the very
      next message runs that call directly with zero provider calls made,
      still through the confirm gate; a non-confirmation, a chat guest's "go
      ahead", and a since-removed tool name are each correctly ignored.
      Verified by `tests/test_finish_signal.py` (rev. 2026-09-22a; canned
      responses, not a live model).

---

# Part G — Test Checklist panel (web console)

> **CURRENT AUDIT — 2026-09-22:** G.1 is delivered and exact current tool coverage is complete: the live tool catalogue and shipped checklist catalogue both contain 170 entries in the audited tree, with no missing or extra checklist names. G.2 remains open because modules can supply many tool entries but only one checklist-group entry. Human browser QA/tightening is still a closure task.


**Status: Delivered.** `jarvis-test-checklist.patch`; see revision
2026-09-20l for the file list. This part didn't exist in earlier revisions of
this plan — it documents a new feature. **G.1 (below) delivered in revision
2026-09-21d** — it is the only numbered item in this part.

**What it is.** **Menu → Test Checklist** is a tester's workbench for every
tool Jarvis can call. Each tool has: what it does, how to test it (`ask`
prompts to type into Ask, and `run` argument sets to run from Debug without
the model), what a pass looks like (`expect`), and optional `needs` / `os` /
`care` / `watch` notes. A tester records a verdict per tool — Untested, Fully
working, Partially working, Not as intended, Bug / broken, Blocked — ticks
individual steps, and adds notes. There are keyboard shortcuts (↑/↓ to move,
0–5 to set a verdict, `/` to search), a verdict/category filter, and an
Overview pane with progress and a "needs attention" list.

**Purely front end.** The catalogue is the static file
`web/public/test-checklist-data.js`; **results live only in the browser's
localStorage** (Export/Import JSON moves them between browsers). Nothing is
written by the CLI or stored under `~/.jarvis`; there is no new server route.
The only request is the read-only `GET /api/tools` Debug already uses, to spot
tools that exist in the CLI but have no entry — those are listed by name only,
marked **NO CHECKLIST**, and named in the Overview's Coverage section. (Since
G.1 that same response also carries any entry a tool's own module supplied,
which the panel merges in.) All
catalogue text is inserted with `textContent`, never `innerHTML`.

**The rule it adds (AGENTS.md → "Test Checklist").** Whenever you add, rename,
remove or change the behaviour of a tool you **must** update its entry in
`test-checklist-data.js` in the same change — a tool change without its entry
is incomplete, like a tool with no schema. Editing a step's text un-ticks it in
testers' browsers (ticks are keyed by the text). The data must be strict JSON
between the `JSON-BEGIN`/`JSON-END` markers.

**Enforcement.** `tests/test_checklist_coverage.py` (5 checks) fails when a
tool has no entry, an entry has no tool, an entry is malformed or duplicates a
step, or a group id disagrees with `tool_registry.TOOL_GROUPS`. Run it directly:
`python3 tests/test_checklist_coverage.py`.

**State after the merge.** The patch shipped entries for the 152 tools that
existed when it was built. The merge added entries for the 16 tools the other
patches introduced (7 `browser_*`, 4 `clipboard_*`, 5 path tools) and two
groups (Clipboard, Browser control); see revision 2026-09-20m. Those 16 are
drafts and worth a human pass. The `clipboard-watch` daemon is not a tool and
has no entry; it is controlled through the existing `daemon_*` tools, whose
entries already exist.

## G.1 Let a tool module supply its own checklist entry — not just gated by whether a custom module's tools happen to be in the shipped file

**Status: Delivered — revision 2026-09-21d, `jarvis-test-checklist-supplied-entries.patch`
(see "As built" at the end of this section; the requirement text below is left
as written).** Originally: new requirement (2026-09-20 addendum).

**The gap.** Today the whole catalogue is one static file,
`test-checklist-data.js`, with every entry keyed by tool name under
`"tools"`, plus a fixed `"groups"` list of categories. Coverage checking
(above, "Purely front end") works by fetching the live catalogue from
`GET /api/tools` and diffing it against that static file's keys — a tool that
exists but has no matching key shows as a bare name, marked **NO CHECKLIST**.
That's the right behaviour for a *shipped* tool someone forgot to document,
but it's the wrong behaviour for a tool that can never have an entry in the
shipped file in the first place: anything `tool_loader.discover_actions()`
picks up from `actions/*.py`, and — more to the point — anything a specific
user wrote themselves through the Custom Tools system (`custom_tools_store.py`,
`~/.jarvis/tools/<name>.py`, editable from the web UI's Custom Tools tab). A
user's own custom tool is by definition unknown to whoever ships
`test-checklist-data.js`; today it shows **NO CHECKLIST** forever, no matter
how good the tool is, because the only place an entry can be authored is a
file that ships with the app. That's the "gated by the available tools" the
owner is pointing at — a custom module's tools are permanently locked out of
ever having a real entry, not just temporarily missing one.

**What to add.** Let a tool module supply its own checklist entry (or
entries) alongside its `TOOL_SCHEMAS`/`TOOLS`/`TOOL_GROUP`, the same way it
can already optionally supply `TOOL_KEYWORDS` and `TOOL_PACK_INSTRUCTION` —
see `actions/_template.py` and `tool_loader.py`'s extraction of those two for
the existing pattern to follow. Concretely:

- A new optional module-level `TEST_CHECKLIST` dict, in the exact same
  per-tool shape `test-checklist-data.js` already uses under `"tools"`
  (`does` / `steps` / optional `needs` / `os` / `care` / `watch`) — an entry
  authored inside a module is byte-for-byte the same shape as one authored in
  the shipped file, so the same validation
  (`tests/test_checklist_coverage.py`'s 5 checks) covers both without a
  second code path.
- **"Per type," not just per tool:** a module introducing a brand-new
  `TOOL_GROUP` — the exact case `_template.py` already calls out ("a
  brand-new group with an empty TOOL_KEYWORDS...") — currently has no way to
  give that group a label/blurb the way the shipped `"groups"` list does for
  every built-in category, so it would show up unlabeled or fall into a
  generic bucket in the panel. Add an optional
  `TEST_CHECKLIST_GROUP = {"label": ..., "blurb": ...}` (or a reserved key
  inside `TEST_CHECKLIST` itself) so a module inventing its own category gets
  a real, named section instead.
- `tool_loader.discover_actions()` extracts this the same way it already
  extracts `TOOL_KEYWORDS`/`TOOL_PACK_INSTRUCTION`: optional, validated
  against the same shape `test_checklist_coverage.py` enforces for the
  static file, and rejected-with-a-log (not a crash) if malformed — matching
  every other optional field's failure mode in that loader.
- The panel needs a way to actually see these at runtime. The static
  `test-checklist-data.js` is loaded as a plain script tag; auto-discovered
  and custom-tool entries only exist inside the running `jarvis` process.
  Extend whatever `GET /api/tools` already returns (or add a sibling route)
  to include each tool's `TEST_CHECKLIST` entry when present, and have
  `test-checklist.js` merge that into `window.JARVIS_TEST_CHECKLIST` at load
  time — shipped entries from the static file, auto-discovered/custom entries
  from the live catalogue, identical rendering either way. A tool with
  neither keeps today's bare-name **NO CHECKLIST** treatment.
- Custom Tools' own starter templates (`custom_tools_store.py`'s point 4,
  "Templates — a starter for each shape of tool") should include a
  `TEST_CHECKLIST` example in every template it hands the user, not just
  document the field in prose somewhere else — the person most likely to
  need this is exactly the person with no reason to have read
  `actions/_template.py` first.

**Documentation — do this well, not as an afterthought.** This needs the
same treatment `_template.py` already gives `TOOL_KEYWORDS`/
`TOOL_PACK_INSTRUCTION`: a clearly marked, example-carrying section in
`actions/_template.py` itself, so hand-written modules (`git_tools.py`-style)
and Custom Tools' generated files both start from the same documented
contract — not just a mention in this plan. Also update
`test-checklist-data.js`'s own header comment (the "FORMAT" section quoted
above) to say explicitly that this is now one of two ways an entry can
exist — shipped here, or supplied by the tool's own module — so someone
reading that file in isolation doesn't conclude it's still the only source
of truth.

### As built (rev. 2026-09-21d)

**Files.** New `jarvis-cli/jarvis/checklist_schema.py`, `tests/test_checklist_supplied.py`.
Changed: `tool_loader.py`, `tools.py`, `custom_tools_store.py`,
`actions/_template.py`, `web/public/test-checklist.js`, `custom-tools.js`,
`test-checklist-data.js`, `tests/test_checklist_coverage.py`, AGENTS.md,
REPO_MAP.md, `web/README.md`. No server route, CLI command or storage was added.

**Against the requirement, bullet by bullet**

| Requirement | Built |
|---|---|
| Optional module-level `TEST_CHECKLIST`, same per-tool shape | ✔ dict of tool name → entry. `group` may be left out (it *is* the module's `TOOL_GROUP`) or given, in which case it must equal `TOOL_GROUP`. |
| "Per type": label/blurb for a brand-new `TOOL_GROUP` | ✔ `TEST_CHECKLIST_GROUP = {"label", "blurb"}` — the plan offered this or a reserved key; a separate name won because it can't collide with a tool called that. Fallback when a group has no label: the panel prettifies the id (`my_group` → "My group"), so a tool is never dropped for lack of a label. |
| Loader extracts it like `TOOL_KEYWORDS`, validated, rejected-with-a-log rather than a crash | ✔ **with one deliberate difference** — see decision 1. |
| Panel sees them at runtime; merged; identical rendering; neither → bare **NO CHECKLIST** | ✔ per-tool `checklist` / `checklist_group` on `tools_list_payload()` items (the list stays a list). `mergeCatalogue()` in `test-checklist.js`. One addition to "identical rendering": a small "From the tool's own file" badge in the detail pane, so a tester knows which file to edit. |
| Custom Tools templates carry an example in **every** template | ✔ all four (`minimal`, `ui`, `ask`, `http`); `test_every_starter_template_carries_a_valid_checklist_example` fails if a new template lacks one. Their group is `custom`, which is now a shipped, labelled group ("Custom tools"). |
| Documentation "done well": `_template.py` section + the data file's FORMAT header | ✔ `_template.py` §8 (why it exists, which home to use, field-by-field, failure mode, where it surfaces) with a live example that a test validates; the data file header now opens with "one of two places an entry can live". |

**Decisions made (change any of them and say so)**

1. **A malformed entry never rejects the tool file.** `TOOL_KEYWORDS` /
   `TOOL_RESULT_SPECS` errors reject the whole module; copying that would take a
   working custom tool offline over a typo in its *test notes*. Instead the entry
   is dropped, discovery logs `[checklist] file.py: <why>`, and — since stderr is
   where nobody looks — the Custom Tools editor's **Check** and **Save** now show
   what was ignored, and which tools have no entry yet. (Persona validation
   already worked this way; it is the closer precedent.)
2. **Shipped wins if a tool is in both places** — and the coverage test *fails*
   on it, so one tool has one home. The panel's tie-break is a backstop.
3. **The payload is the transport**, not a sibling route: fewer moving parts, and
   no new CLI verb to reserve in the three diverging `RESERVED_NAMES` sets. Cost:
   `/api/tools` grows by the size of the supplied entries, bounded by the caps in
   `checklist_schema.py` (all roughly 2× or more the longest shipped field).
4. **`window.JARVIS_TEST_CHECKLIST` is not mutated.** The plan said to merge into
   it; the panel instead keeps that global as exactly the shipped file and rebuilds
   a merged view (`JarvisTestChecklist.catalogue()`) on every live read. Otherwise
   deleting a custom tool and pressing Refresh would leave its entry behind.
5. **Stricter than before, for both homes:** unknown keys (`watchs`, `need`) and
   unknown step keys are now errors, and there are length caps. All 170 shipped
   entries already conform; the point is that a typo in a custom tool's entry is
   caught instead of silently rendering nothing.
6. **Untrusted input:** an entry is plain data from a file a person wrote, but it
   is still checked to be JSON-serialisable before it can reach
   `jarvis tools-list` (one object() in a `run` block would otherwise break the
   Debug panel and the checklist for everyone), and the panel re-checks shape
   before rendering. Everything is inserted with `textContent`, as before.

**Tests.** `tests/test_checklist_supplied.py` (18 checks): extraction and
group defaulting; a matrix of 13 malformed entries all dropped-and-logged with
the tool still registering; a rejected file contributes no entry; group labels;
end-to-end from a real `~/.jarvis/tools` file to the `tools-list` payload (only
the supplying tool gains the keys; junk is dropped and the payload stays JSON);
the editor's check/write report; every template; `_template.py`'s example; and
the JS merge under node (skipped if node is absent) including "shipped data never
mutated" and "junk from the CLI can't throw". `test_checklist_coverage.py` now
counts entries from both homes, validates through `checklist_schema`, and adds
"no tool in both places" (6 checks, was 5). A mutation check confirmed the
payload test fails if the field is not emitted.

**Gotchas found**
- `discover_actions()` caches imported modules in `sys.modules` by file *stem*,
  so a test that discovers two different sources under the same filename gets the
  first module back both times (looks like "my feature does nothing"). The new
  tests use a unique filename per case.
- `custom_tools_store.validate_source()` re-implements the loader's contract
  checks by hand instead of calling `tool_loader._validate`, so any new optional
  field has to be wired into **both**. G.1 does; the next one will need to as well.

**Not done / open**
- Not opened in a real browser (see the caveat in 0.0).
- The 16 first-draft entries from rev. 2026-09-20m (7 `browser_*`, 4
  `clipboard_*`, 5 path tools) are still drafts; they need someone to run each
  test and tighten the text, which no patch can do.
- No shipped tool was migrated to its own module's `TEST_CHECKLIST`; the 170
  shipped entries stay in the data file. Migration would be optional and
  mechanical, but has no payoff until someone wants entries next to code.
- A stale entry in a *custom* tool cannot show the panel's "Not in CLI" badge
  (its entry only exists while the tool does), which is the right behaviour.


---

## G.2 A module — custom tools included — can already supply more than one *tool* entry; make that explicit, and give it more than one *group* entry too

**Status: not started — new requirement (2026-09-21e addendum).**

**What already works, and isn't the gap.** `TEST_CHECKLIST` is a dict keyed
by tool name, so nothing in G.1's schema caps a module at one entry — a
module that registers several tools can already give each of them its own
`does`/`steps`/`expect` entry. The built-in example proves it today:
`browser_tools.py` is one module that supplies **one** `TEST_CHECKLIST_GROUP`
(the "browser" category — label + blurb) and **seven** separate
`TEST_CHECKLIST` entries, one per tool (`browser_goto`, `browser_click`,
`browser_fill`, `browser_get_text`, `browser_screenshot`, `browser_wait_for`,
`browser_close`). G.1's own opening line already says a module can supply
"its own checklist entry (**or entries**)" — the *tool*-entry side of this
was never actually limited.

**The gap.** Two places this isn't explicit enough:

1. **For custom tools specifically**, nothing says so in words a
   non-implementing user would read. The Custom Tools system's own
   convention — one file per tool (`~/.jarvis/tools/<name>.py` or `jarivs-cli/jarvis/actions/<name>.py`), one
   starter template per "shape of tool" (`minimal`/`ui`/`ask`/`http`) — makes
   it easy to assume a custom module is capped at one tool and therefore one
   `TEST_CHECKLIST` entry. Nothing enforces that cap, but nothing tells the
   user it isn't there either. If a hand-written custom module legitimately
   defines more than one tool in its `TOOLS` dict, it should supply a
   `TEST_CHECKLIST` entry for each one, exactly the way `browser_tools.py`
   does — this needs to be said in prose, with the `browser_tools.py`
   precedent named, not left to be inferred from the dict's type.
2. **`TEST_CHECKLIST_GROUP` itself is still singular per module** — the
   as-built decision in G.1 requires `group` (when given) to equal the
   module's one `TOOL_GROUP`. A module whose tools genuinely split across
   more than one logical category — plausible for a larger custom module, or
   a future built-in one — has no way to give each half its own labelled
   section; everything it supplies lands under one group whether or not that
   groups them sensibly. Let a module supply more than one
   `TEST_CHECKLIST_GROUP`-shaped entry, each tagged with the subset of its
   own tool names it covers, instead of exactly one tied 1:1 to `TOOL_GROUP`.
   The single-group shape stays the default for the common case (one module,
   one category); this only removes the cap for the module that needs more.

**Documentation — do this well, same as G.1.** Whoever builds this must
update **both** of the places G.1 already treats as the documentation
contract: this section (done — this is that update) and
`actions/_template.py`'s §8 (the `TEST_CHECKLIST`/`TEST_CHECKLIST_GROUP`
section G.1 added there). Add, in `_template.py`:
- An explicit line stating a module may supply as many `TEST_CHECKLIST`
  entries as it has tools, with a two-tool example (not just one), so a
  reader never has to infer this from the schema's type.
- The multi-group shape once built, with a short example showing a module
  splitting its own tools across two labelled sections.
Also touch `test-checklist-data.js`'s own header comment again, the same way
G.1's documentation bullet did, if the multi-group shape changes anything a
reader of that file in isolation would otherwise get wrong.

**Tests, once built.** Extend `tests/test_checklist_supplied.py` (which
already covers the one-group-many-tools shape via `browser_tools.py`-style
fixtures) with: a fixture module defining 2+ tools and confirming each gets
its own entry (regression-proofing what G.2 documents, not new behaviour);
and, once the multi-group shape lands, a fixture module supplying two groups
and confirming each tool's entry resolves to the right one.

---

## Revision 2026-09-20 (F wrap-up)

Patches, apply in this order on top of the earlier F patches: `jarvis-failover-transcript.patch` (F.7), `jarvis-key-health.patch` (F.9, F.8 item 3), `jarvis-log-labels.patch` (F.12 labels), `jarvis-router-last-line-highlight.patch` (F.10 item 3 + highlight). Behaviour changes worth knowing: a 503 now skips the provider's remaining keys and demotes it ~45 s; a 429 message ends `[retry in Ns]`; long multi-line messages get a +2 last-line routing bonus. Guesses not yet checked against real logs: Groq's call-in-reasoning shape; Gemini accepting a merged trailing user turn. Full suite passes except the known `test_subagents` bug.

---

# Part H — UI: Daemons/Backlog/Log search rework, a slighter Schedules pass, and a notification on job creation

> **CURRENT AUDIT — 2026-09-22:** This part is **partial/rework-open, not “not started.”** The repo already has daemon, backlog, log-search and schedule panels with functioning baseline behavior. For example, the backend tracks daemon restart counts and the current front end has command-step drag/reorder; however, the requested Test-Checklist-quality redesign of these panels is not complete. The remaining work is mostly UI architecture/interaction quality plus log-search behavior, not creation of brand-new backend subsystems.


**CURRENT STATUS: PARTIAL / REWORK OPEN.** A functioning first-pass implementation already exists in the current repo; the requested high-quality rework remains. Raised directly by the owner, who called out the Test Checklist panel (Part G) by name as the quality bar: "it has to be high quality just like the test checklist UI."

## H.1 Daemons / Backlog / Log search: full rework to the Test Checklist bar

**Where these live today.** All three are still built the same way every panel
before Test Checklist was: markup inline in the ~80KB `index.html`
(`daemons-overlay`, `backlog-overlay`, `logsearch-overlay`), behaviour inside
the one ~420KB `app.js`, styling inside the one ~125KB `style.css`, all reusing
the generic `.debug-overlay`/`.debug-panel`/`.skills-pane` chrome the Debug and
Skills panels also use. Test Checklist is the one panel that broke from this:
its own `test-checklist.css`, `test-checklist.js`, and a data file
(`test-checklist-data.js`), with a real information architecture on top —
verdict/category filters, `/` search, keyboard shortcuts (↑/↓, 0–5), and an
Overview pane with progress and a "needs attention" list (Part G). That
difference in investment is exactly what the owner is pointing at.

Some hover/transition polish already landed on Backlog's `.kanban__card` and
Log search's `.logsearch-hit` (see the comments already in `style.css` around
those rules — cards used to be static with no hover feedback at all). That
polish is a good sign of direction but is not the same thing as the rework
being asked for here: none of the three has Test Checklist's dashboard pane,
keyboard shortcuts, or live-filtering search, and none has its own `.css`/`.js`
file.

**Goal.** Bring Daemons, Backlog, and Log search up to the same bar as Test
Checklist — a tester who has used the Test Checklist panel should not be able
to tell, from polish alone, that these three are older. Concretely, that
means (at minimum):

- **Split each into its own files**, the way Test Checklist did — e.g.
  `daemons.css`/`daemons.js`, `backlog.css`/`backlog.js`,
  `logsearch.css`/`logsearch.js` — rather than adding more weight to the
  shared `app.js`/`style.css`. Keep the markup each panel needs in
  `index.html` (Test Checklist's own overlay markup still lives there too;
  only its behaviour and data are split out), and check how `index.html`
  currently loads `test-checklist.js`/`.css` for the loading-order convention
  to follow (see the "Scripts load LAST" comment already in `index.html`
  around line 1485) — a panel that fires its own init before the DOM/other
  panels are ready has been the actual cause of bugs before (see that same
  comment's cross-reference).
- **Daemons:** a per-row live-state indicator (colour dot: running / stopped /
  crashed) rather than only the single `daemons-status-line` summary; a
  console (`daemon-console`) that distinguishes stdout / stderr / crash
  traceback instead of one flat `<pre>`; keyboard shortcuts to move between
  services and scroll the console; and a crash-loop indicator (a service that
  has restarted N times recently) surfaced in the list itself, not something a
  tester has to find by reading the raw console. Check whether the daemon
  status route already reports a restart count before adding one.
- **Backlog:** keep the kanban board (`backlog-board`, drag-reorder already
  works per the style.css comment — audit, don't reimplement), but add the
  Overview-pane treatment Test Checklist has: counts per column, a "blocked"
  roll-up, filter by project, and a search box that filters cards live. None
  of that exists today — `backlog-board` just renders whatever `backlog_list`
  returns, unfiltered.
- **Log search:** highlight the query term inside each `.logsearch-hit`
  (today the whole line renders unhighlighted); live search-as-you-type the
  way Test Checklist's `/` search works, instead of only searching on the
  `btn-logsearch` click; and a way to jump from a hit straight into the
  relevant Daemons console or Ask conversation instead of a dead-end result.

**Scope guard.** This is a presentation/interaction rework on top of what
already exists server-side — Daemons/Backlog/Log search talk to real routes
(daemon status/control, backlog CRUD, raw log grep), unlike Test Checklist's
read-only `localStorage` design (Part G), so that "purely front end" framing
does not carry over automatically. Don't change the server routes or the
`daemon_*`/`backlog_*` tool contracts as part of this unless the rework
genuinely needs data a route doesn't return yet (the daemon restart count
above is the one already-flagged candidate) — call those out explicitly
rather than quietly widening scope.

## H.2 Schedules panel: a slighter pass

Explicitly **not** the same scope as H.1 — the owner asked for "a slight
rework," not a rebuild. Current state: `sched-overlay` is plain
`.menu-overlay`/`.menu-panel` chrome, `sched-list` is a bare
`.skills-list`/`.skills-pane--list`, and the add row (`sched-when`/
`sched-message`) is the only interaction beyond the list itself — no grouping
by kind (reminder / notify / task / watch), and nothing at a glance
distinguishing a one-shot job from a recurring one, or a healthy job from a
paused/failing one, without opening it.

To do, scoped narrowly:

- Group or filter `sched-list` by job kind.
- Show next-run time and recurrence directly on each row, not only in a
  detail view.
- Visually distinguish a paused/failed job from one running normally.

Keep this inside the existing `app.js`/`style.css` rather than splitting into
dedicated files — unless, once work starts, splitting genuinely turns out to
be the easier path (e.g. because it ends up sharing components with the H.1
rework).

## H.3 Every scheduled-task creation should send a confirmation notification

**Current behaviour.** `tool_schedule_task`, `tool_remind_me`,
`tool_schedule_watch`, and the `when`-given branch of `tool_notify_me` (all in
`scheduler_tools.py`) create the job through `scheduler.create(...)` and
return `_ok(job)` straight to the caller — none of them calls
`notifier.notify(...)` at creation time. The only notification a job produces
today is when it actually fires, later. So confirmation that something got
scheduled exists only as text in that one chat turn's reply; nothing reaches
the user's other channels (whatever `notifier.CHANNELS` covers), and nothing
is recorded in Notifications history marking that the job was set up in the
first place — only that it later ran.

**To do.** After `scheduler.create()` succeeds in each of
`tool_schedule_task` / `tool_remind_me` / `tool_schedule_watch`, fire a
`notifier.notify(...)` confirming the job was created: title along the lines
of the job's own title, message summarizing what + when — e.g. "Reminder set
for tomorrow 9am: <message>." Reuse `_channels(args)` (already used to route
the job's own eventual notification) so the confirmation goes to the same
places. Whether `tool_notify_me`'s `when`-given branch gets the same treatment
is worth deciding explicitly — it's the same code path as the other three, so
consistency argues for it.

**One thing to get right:** `tool_notify_me`'s `when`-*omitted* branch already
calls `notifier.notify(...)` directly to deliver the message right now — that
one must NOT also get a "scheduled" confirmation layered on top, since nothing
was scheduled; it already **is** the notification.

**One thing to decide with the owner:** whether this confirmation should be
suppressible (e.g. `args.get("confirm", True)`) for a subagent or automated
flow that creates several jobs back-to-back and doesn't want a notification
per job — a burst like that could otherwise spam every channel at once.
Default to sending it; make it optional if it turns out to be noisy in
practice.

**Related work already on the list:** route this through the same "audit
every place that creates a notification" pass D.2 already calls for — check
whether these new creation-time notifications need the same
summary/raw-JSON stripping D.2 describes for fire-time notifications.

---

# Part I — Chat UI: code blocks with one-click copy, the `/` command palette, and bugs found on the way

> **CURRENT AUDIT — 2026-09-22:** Still open. No requested code-block renderer/one-click code-copy feature or new `/` palette engine has landed in the current archive. The two high-value bugs I-B1 and I-B2 remain represented in the source: `extractMath()` processes raw text before Markdown/code rendering, and reserved command names are still maintained in separate CLI/web/config lists.


**CURRENT STATUS: OPEN.** The requested implementation is still absent from the current archive. The two reproduced bugs (I-B1/I-B2) remain actionable; the source already has legacy rendering/autocomplete code but not the planned rework.

Two owner requests, both about the Ask panel (the composer and the reply thread), plus the bugs turned up while researching them:

1. **Code markdown.** Code in a reply should render as a real code block — language label, syntax colouring, and a **seamless copy**: one click, exactly the code, no selecting.
2. **`/` commands.** Add new entries to the `/` commands in the front end, and **rework the current `/skillload` autocomplete** ("it's bad"): a much better UI, and autocomplete for *everything* — every existing command, not only skill names. The one new entry the request implies by name is **`/copy`** (copy the last reply, or one chosen code block, from the keyboard — it is where the two halves meet). Any other new verbs are decision D-I5.

**How this was checked.** Everything below was read out of `jarvis-main_39_.zip`. Claims marked **ran** were executed in the sandbox (Node 22 for the JS, Python 3 for the CLI); **read** means from source only; **not verified** means it needs a real browser or a real Windows box, which the sandbox doesn't have (no browser, no network to the CDNs — the same caveat `tests/verify_math_rendering.js` states). Line numbers are for that tree; re-check if files moved.

## I.0 Bugs found while researching this part

| ID | Bug | Checked | Detail / fixed by |
|---|---|---|---|
| **I-B1** | Math extraction swallows code: a `$$`, a `\[`…`\]`, or a pair of `$` inside code merges code blocks or breaks inline code | **ran** | I.0.1 — small, independent, do first |
| **I-B2** | A saved command named like a built-in subcommand is accepted by both validators, then silently shadowed by the built-in; 30 built-ins are on no reserved list at all | **ran** | I.0.2 — small, independent |
| **I-B3** | `/skillunload <anything>` reports "unloaded", even for a name that was never loaded or installed | read | I.2.5, I.2.8 |
| **I-B4** | With the suggestion list open, Enter sends the half-typed command (the composer's only key handler is Enter → submit) | read | I.2.5 |
| **I-B5** | A mistyped `/verb` goes to the model as an ordinary message — only two patterns are intercepted | read | I.2.6 |
| **I-B6** | The suggestion box is in normal page flow, so opening it shifts the layout; its own HTML comment says CSS positions it | read | I.2.5 |
| **I-B7** | `/skillunload` completes from every installed skill, not the loaded ones; invalid skills are offered like valid ones; the list comes from a cache refreshed in only two places | read | I.2.8 |
| **I-B8** | The suggestion list has no keyboard navigation and no ARIA | read | I.2.5 |
| **I-B9** | Copy buttons have no fallback when `navigator.clipboard` is missing or refused | read; **not verified** in a browser (latent — the console binds 127.0.0.1, a secure context) | I.1.3 |
| **I-B10** | Three render sites repeat the same three calls; the pending bubble is rebuilt from scratch on every line | read (partly noted in §8.5) | I.1.3 |
| **I-B11** | Speak, and the automatic speak-back after a mic turn, read fenced code aloud | read | I.1.3 |
| **I-B12** | In Focus layout the Commands panel is hidden and Menu has no Commands item, so saved commands look unreachable | read (markup/CSS); **not verified** in a running UI | I.2.3 (`/run`) |

### I.0.1 I-B1 in detail — `extractMath` runs on the raw text, so it eats code

**Cause.** `extractMath` (`app.js:2693`) runs on the *raw* reply, before marked, and doesn't know what code is. Its block patterns (`$$…$$`, `\[…\]`) use `[\s\S]+?`, so they match across lines and paragraphs; its inline `$…$` pattern matches across backticks on one line. `restoreMath` puts the exact source back afterwards, so a match that swallowed only ordinary code is invisible — but when the swallowed span contained a closing fence or inline-code backticks, marked has already parsed a document that no longer has them.

**Repro.** I sliced `extractMath`/`restoreMath` out of `app.js` exactly as `tests/verify_math_rendering.js` does and ran four inputs through them plus `marked` (**ran**; locally marked 18.0.2, production loads 15 — the damage happens in `extractMath` before marked sees the text, so the version shouldn't matter, but the rendered symptoms below were observed on 18 only):

| Case | Input (abridged) | Observed |
|---|---|---|
| A (control) | a bash fence containing `echo "$HOME and $PATH"` | Fine — the stashed span `$HOME and $` is restored byte-exactly |
| B | a fence containing `echo $$`, prose after it, and a later inline `` `$$` `` | The stash swallows `$$`, the **closing fence**, the prose between, and `` `$$ ``. The fence never closes, so **everything after it renders as one giant code block** |
| C | `` Use `echo $HOME` then `echo $PATH` to compare. `` | One code span reading `echo $HOME` then `echo $PATH` with literal backticks inside it — **inline-code styling breaks** |
| D | a js fence containing `/\[(.*)/`, prose, then a second js fence containing `/(.*)\]/` | **The two fences merge into one `<pre>`**, with "Text between." and the second fence's backticks shown as literal text inside it |

**Why it matters here.** Shell `$$`, `$1 … $2`, and regex `\[`…`\]` are ordinary content in exactly the replies where code blocks matter — and a merged or broken block would also break the Copy button this part adds (it would copy the merged text).

**Fix** (small, independent — §0.4 step 1c).

- Make extraction **segment-aware**. Split the raw text into code and non-code segments — fenced blocks first (an opening fence of 3+ backticks or tildes with at most 3 spaces of indent, closed by a line of the same character at least as long; **an unclosed fence runs to the end of the text**, which is also what §8.5's streaming requirement needs), then inline code spans (a backtick run closes at the next run of *exactly* the same length) — and apply the four math patterns only inside non-code segments. Placeholders keep one running index across segments; `restoreMath` doesn't change.
- Applied per segment, no math span can cross a code boundary. Accepted trade-off: math that *contains* a code span (`$a `b` c$`) is no longer treated as math — unusual, and today's behaviour for it is already wrong.
- Keep the new helper inside the source range `tests/verify_math_rendering.js` slices (`const MATH_PLACEHOLDER_OPEN` … `function renderMathIn`), or move the marker in the same change — otherwise the test silently keeps running the old code.
- Tests, added to `tests/verify_math_rendering.js` (which already needs `npm install marked@15`): cases A–D; `~~~` fences; a longer closing fence; an indented fence; an unclosed fence; math adjacent to inline code (`$x$ and `code` and $y$` keeps both math spans and the code); and the existing real-math cases (`$2*x + 3*y$`, `\(x_B = 1\)`, `\[ … \]`) still pass.
- Patch: `jarvis-math-extract-skips-code.patch`.

### I.0.2 I-B2 in detail — saved-command names vs built-in subcommands

Three hand-kept lists of "reserved" names disagree, and none of them is the real dispatch table:

| Copy | Where | Size |
|---|---|---|
| `RESERVED_NAMES` | `jarvis-cli/jarvis/cli.py:46` | 116 names (plus the `then`/`and` separators and `-h`/`--help`) |
| `RESERVED_NAMES` | `web/server.js:29` (used by `validateName`, `server.js:172`) | 59 entries |
| `RESERVED_NAMES` | `jarvis-cli/jarvis/commands_config.py:14` (used by `validate_command_name`) | 20 entries |

- **Ran:** `commands_config.validate_command_name` returns `None` (valid) for `backlog`, `daemons`, `skillload`, `think`, `organize-json`, `logs-search` and `mode`; only `config` was refused of the names I tried. `web/server.js`'s smaller set also accepts `backlog`, `daemons`, `skillload` and `think` (**read**).
- **Ran:** with a saved command called `think` in `~/.jarvis/commands.json`, `jarvis think` prints the collision warning from `cli.py:145–151` and then **runs the built-in** — it printed the thinking-level JSON, not the saved command. The web console runs a saved command by spawning the CLI with its name (`buildArgv`, `server.js:1905`), so Execute on such a command would run the built-in too (**read**).
- **The CLI's own list is incomplete.** Scanning `cli.py`'s dispatch (`argv[0] == "…"` and `argv[0] in (…)` literals — a regex scan, so a different dispatch shape could be hiding more) found **30 public subcommands that aren't in `RESERVED_NAMES`**: `doctor`, `version`, `conv-export`, `browser-setup`, `policy`, `policy-check`, `policy-dry-run`, `memory-ns`/`-list`/`-consolidate`/`-recall`/`-reindex`/`-stats`, `calendar-add`/`-list`/`-remove`/`-events`, `digest-status`/`-on`/`-off`/`-now`/`-preview`, and `ctools-list`/`-show`/`-write`/`-check`/`-delete`/`-run`/`-toggle`/`-templates` (plus the internal `_internal_retitle`). A saved command named `doctor` is shadowed with **no warning at all**. The three delegated modules (`workspace_cli`, `channels_cli`, `clipboard_cli`) are fully covered — **ran**, every name in their `COMMANDS` sets is in `RESERVED_NAMES`.
- **It contradicts a stated rule.** `REPO_MAP.md` §6 says `web/server.js` "never reimplements a rule — every route shells out". The web's own copy of the reserved list is exactly a re-implemented rule.

**Fix** (its own small patch, independent of the chat-UI work — and the foundation the palette's coverage guard stands on, I.2.9).

1. One canonical set in a new tiny module (e.g. `jarvis/reserved_names.py`) that both `cli.py` and `commands_config.py` import. (`cli.py` doesn't import `commands_config` today — only a comment mentions it — so there's no import cycle to worry about.)
2. `web/server.js` stops keeping a copy: name validation on `POST`/`PUT /api/commands` asks the CLI (a small `commands-check-name <name>` subcommand, itself added to the canonical set).
3. A drift test, `tests/test_reserved_names.py`, that fails when `cli.py` dispatches a name the set doesn't contain — regex-extract the dispatch literals and import the three delegated `COMMANDS` sets, as was done for this finding.
4. Optional: a `jarvis doctor` check that lists saved commands a built-in shadows, since existing `commands.json` files may already contain some. Nothing is auto-renamed — that's the owner's call (D-I9).

Patch: `jarvis-reserved-names-single-source.patch`.

## I.1 Code blocks and seamless copy

### I.1.1 What exists today (read)

- **Renderer.** `renderMarkdown()` (`app.js:2740`): `extractMath` → `marked.parse(…, { breaks: true, gfm: true })` → DOMPurify → `restoreMath`. marked@15, DOMPurify@3 and KaTeX 0.16.11 load from jsdelivr (`index.html:1498–1501`). **No syntax highlighter is loaded**, so a fence is a bare `<pre><code class="language-x">`.
- **Styling.** `style.css:1475–1494`: a bordered box with `overflow-x: auto`, scoped to `.ask-msg--jarvis`. No language label, no header, no height limit, no copy.
- **Copy today.** The per-message **Copy** (`copyAskRaw`, `app.js:2993`) copies the whole reply as raw markdown (`msg.dataset.raw`); the selection pop-up (`#ask-sel-copy`, `4211`) copies highlighted text. Both call `navigator.clipboard.writeText` and on failure only toast (I-B9). Getting one block means drag-selecting inside a horizontally scrolling `<pre>`, or copying the whole reply and trimming.
- **Your own messages.** `addUserBubble` (`3039`) inserts the text as plain text; a fenced block you paste isn't rendered.
- **Three render sites**, each `renderMarkdown` → `linkifyPaths` → `renderMathIn`: pending (`rerenderAskPendingBubble`, `4021`), finalize (`4069`), history (`addJarvisStaticBubble`, `6318`). Add a step at one and not the others and a live reply looks different from the same reply reloaded (I-B10).
- **The pending bubble** is replaced wholesale (`innerHTML =`) on every appended line, so per-block state — a "Copied" tick, a collapsed state, the scroll position inside a wide block, a text selection — dies each time; per token once §8 lands, which §8.5 already flags as O(n²).
- **Neighbours to preserve.** `linkifyPaths` (`2894`) skips `<pre>` but rewrites a path-looking inline `code` into a `.path-link` span; `renderMathIn` ignores `pre`/`code`; the selection pop-up hides on thread scroll (`4202`); `data-raw` keeps the raw markdown for Copy, Redo and Speak.

### I.1.2 What's wanted

- **R1** Fences (backticks and `~~~`) render as blocks with a language label, syntax colouring when the language is known, and a line count.
- **R2** One-click **Copy** on every block, in place, with the feedback in the button (no toast), operable from the keyboard.
- **R3** Copy yields exactly the source: no label, no line numbers, no trailing-newline artefact. (Shell prompts: D-I3.)
- **R4** Selecting inside a block and pressing Ctrl+C also yields clean code — the chrome isn't selectable.
- **R5** Copy works when `navigator.clipboard` is missing or refused (a fallback), through one helper every copy button in the Ask panel uses.
- **R6** Long blocks collapse with "Show all N lines"; wide lines scroll (or wrap, per a persisted toggle); the bar stays reachable while scrolling a tall block. Collapsing never changes what Copy copies — the whole block.
- **R7** Identical in live replies, reloaded history and (later) streamed replies; a half-finished fence never spoils the rest of the message (§8.5).
- **R8** Themed by skin/persona variables; nothing hard-coded except fallback values.
- **R9** No new injection surface: the chrome is built by JS after sanitising, and model-authored HTML can't forge a working button.
- **R10** Graceful degradation: highlighter blocked or offline → plain monospace, Copy still works.
- **R11** Fenced code in your own bubble renders too (D-I2).
- **R12** `/copy` gives the same from the keyboard (I.2).

### I.1.3 Design

**DOM** — built by JS after DOMPurify, never part of the HTML string:

```
<div class="codeblock" data-lang="python">
  <div class="codeblock__bar">          <- outside <pre>: not selectable, never copied
    <span class="codeblock__lang">python</span>
    <span class="codeblock__meta">24 lines</span>
    <button class="codeblock__wrap" type="button">Wrap</button>
    <button class="codeblock__copy" type="button">Copy</button>
  </div>
  <pre><code class="language-python">...</code></pre>
  <button class="codeblock__more" type="button">Show all 84 lines</button>   <- only when collapsed
</div>
```

```
 ┌ python ─────────────────────────────── 24 lines   Wrap   [ Copy ] ┐
 │ def f(x):                                                         │
 │     return x + 1                                                  │
 └───────────────────────────────────────────────────────────────────┘
```

**One entry point (I-B10).** `renderRich(bubbleEl, markdown, { final })` = `renderMarkdown` → set `innerHTML` → `linkifyPaths` → `renderMathIn` → `enhanceCodeBlocks`. All three render sites call it, so a live reply and the same reply reloaded can't diverge. `enhanceCodeBlocks` is DOM post-processing like `linkifyPaths`, so DOMPurify never sees it and needn't allow buttons. It is **idempotent** (it skips a `<pre>` already inside a `.codeblock`), so §8's incremental renderer can re-run it on just the tail.

**One copy helper (I-B9).** `copyText(text)` tries `navigator.clipboard.writeText`; on absence or rejection it falls back to a temporary off-screen `<textarea>` plus `document.execCommand("copy")`, restoring the focus and selection the composer had, and returns a boolean so callers show "Copied" or "Couldn't copy". `copyAskRaw` and `#ask-sel-copy` move onto it. It is the **browser's** clipboard on purpose — not Part B's `clipboard_set`, which writes the server's OS clipboard and would be a network round trip to do what the page can do itself.

**What Copy copies.** The `<code>` element's `textContent` at click time — highlighting only wraps text in spans, so `textContent` is still the original source — with exactly one trailing newline removed (marked appends one). Never a `data-` attribute: model-authored HTML can carry any attribute it likes. Line endings are untouched. Collapsed or not it copies the whole block, and the button says "Copy 84 lines" on a collapsed one so that isn't a surprise.

**Click handling — delegated and unforgeable.** One listener on `#ask-thread` (bubbles are rebuilt through `innerHTML`, so per-button listeners would be lost). It acts only on buttons inside a wrapper the enhancer created, tracked in a JS `WeakSet`. DOMPurify's default profile allows `<button>` and `class`, so a model-authored `<button class="codeblock__copy">` in a reply must not become a live control — a class name or `data-` attribute can be forged, a `WeakSet` entry can't.

**Clean selection (R4).** The bar sits outside `<pre>` with `user-select: none`; line numbers, if ever added, are CSS counters and never text nodes; soft-wrap adds no characters; highlight spans add none. Test: select-all inside a block, paste, compare with the source.

**Highlighting.** highlight.js — core plus a common-languages bundle — from jsdelivr, pinned to an exact version and guarded by the same `typeof … === "undefined"` degradation marked and KaTeX already use (D-I1 covers vendoring it instead). We ship **our own token CSS** — `.hljs-keyword`, `-string`, `-comment`, `-number`, `-title`, `-built_in`, `-addition`, `-deletion`… — mapped to `--code-*` variables whose defaults derive from the skin's accent, rather than a stock theme sheet, which would ignore skins and persona themes (`JarvisUI.themes`). Rules: highlight only when a language is given *and* known (an alias map: js, ts, py, sh, ps1, yml…); never `highlightAuto` on unlabelled blocks (slow, often wrong); skip blocks over a size cap (roughly 100 KB — measure first), which stay plain but still copyable; highlight once per block, and only after its fence is closed. The library's output is escaped HTML; it goes in via `innerHTML` and is re-sanitised through DOMPurify restricted to `span[class]`, so "nothing unsanitised reaches the DOM" stays true.

**Collapse, wrap, sticky bar.** Above about 30 lines a block collapses to about 16 with a fade and "Show all N lines" (a class toggle, no re-render). **Wrap** toggles `white-space: pre-wrap`, persisted in `localStorage` (`jarvis.code.wrap`, wrapped in try/catch like the other localStorage uses). The bar is `position: sticky; top: 0` inside the thread's scroll container, so Copy stays reachable in a tall block.

**Streaming (§8.5).** An unclosed fence at the end of the text is a *streaming* block: the bar shows "writing…", **Copy is disabled** (so half a block isn't copied by accident), and there's no highlighting. When the fence closes — or on `ask-exit`, when the final text replaces the streamed text — it is enhanced and enabled. Until §8 lands, `rerenderAskPendingBubble` still rebuilds per line: the pending render draws the bar with Copy disabled and no highlighting, and `finalizeAskBubble` does the full pass, so nothing flickers. The `extractMath` fix (I-B1) already makes an unclosed fence safe.

**Your own bubble (R11, D-I2).** Split the user's text on fences: prose runs stay `textContent` (a `**` in your message shouldn't turn bold — the plain-text bubble is deliberate), fences go through the same block builder. Narrower than switching user bubbles to markdown.

**`/copy` (R12).** `JarvisRich.blocksIn(msgEl)` returns `[{ lang, text, lines }]` from a message's enhanced wrappers. `/copy code` lists them (language · line count · first line) and copies the chosen one; `/copy` copies the whole reply's raw markdown, same as the message's Copy button.

**Speak (I-B11).** `speakText` receives `msg.dataset.raw` — the whole markdown — from the Speak button (`app.js:2986`) and from the automatic speak-back after a mic turn (`4090`), so code is read aloud symbol by symbol. Strip fenced blocks (saying "code block omitted") before TTS. A few lines; optional but adjacent.

### I.1.4 Files and tests

- **New:** `web/public/rich-text.js` — `enhanceCodeBlocks`, `copyText`, `blocksIn`, the fence splitter and the pure helpers, none of which need `app.js`'s closure — and `rich-text.css` (bar, collapse, tokens). It loads **before** `app.js`, like `ui-kit.js`, and depends on nothing in it. `app.js` keeps `renderMarkdown`/`extractMath` (the math fix lives there) and gains `renderRich`.
- **Node tests** in the style of `tests/verify_math_rendering.js` (slice by marker, plain asserts, no framework): `tests/verify_code_blocks.js` for `normalizeLang`, `codeTextForCopy` (trailing newline, D-I3), `splitFences` and the speech-stripping helper.
- **Not testable in the sandbox** (no browser, no jsdom, no network): real selection-copy, the clipboard permission path, DOMPurify, highlight.js, the token colours in each skin, sticky behaviour, streaming. Those go on the manual pass in I.5.
- **Docs:** `REPO_MAP.md` §6 gets a paragraph; `web/README.md` a short "code blocks" note.
- **Not now:** Mermaid/diagram fences; download-as-file; line numbers (if ever, CSS counters); the same chrome in the dev-agent bubble (`app.js:3724`), console bubble (`3901`), subagent transcript and Logs views — each can call `enhanceCodeBlocks` once it exists.

## I.2 The `/` command palette

### I.2.1 What exists today (read)

- **Four local verbs** — `/skillload <name>`, `/skillunload <name>`, `/skillmake`, `/skilladd` (`SKILL_SLASH_RE`, `app.js:5757`; handler `5812`) — and the slash-less `organize-json <path>` (`ORGANIZE_JSON_RE`, `4228`). Both intercepts sit in the ask-form submit handler (`4492` and `4501`), before the `state.running` check, and only when no quote is attached. That is the entire local command surface.
- **The autocomplete.** `SKILL_SUGGEST_RE` (`5758`) fires only after `/skillload␠` or `/skillunload␠`; `updateSkillSuggest` (`5777`) substring-filters a cached list of skill names, first 8, into `<button>`s inside `#skill-slash-suggest` (`index.html:371`), activated on `mousedown` (`5796`).
- **Why it's bad**, one line each (details in I.0): `/` and `/sk` show nothing so the verbs are undiscoverable; it only knows skills; Enter sends the half-typed command (I-B4); a typo goes to the model (I-B5); opening it shifts the layout (I-B6); names only, although `/api/skills` already returns descriptions, validity, version and reference files; `/skillunload` can't show what's loaded and reports success for anything (I-B3, I-B7); no keyboard navigation, no ARIA (I-B8).

### I.2.2 What's wanted

- **P1** Typing `/` at the start of an empty composer opens a **palette** listing every command, grouped, with descriptions; typing narrows it.
- **P2** After a verb and a space, a second level completes the verb's argument from live data, with context — descriptions, state, badges.
- **P3** Full keyboard support; mouse and touch too.
- **P4** A floating overlay: no layout shift.
- **P5** Covers **every existing command** (I.2.3, I.2.4) — enforced by a test, not by good intentions.
- **P6** New verbs wherever the UI already has a control but no typed form, plus `/copy` (ties to I.1).
- **P7** Local commands never reach the model and never cost tokens; a near-miss typo isn't silently sent to it.
- **P8** Existing muscle memory keeps working: `/skillload x`, `/skillunload x`, `/skillmake`, `/skilladd` and `organize-json path` behave as today.
- **P9** Accessible (combobox/listbox), themed, working in Classic and Focus layouts and at phone width.
- **P10** One registry is the single source of truth for the palette, `/help` and the coverage test.

### I.2.3 Command inventory (35 verbs)

"Replying" = may it run while Jarvis is mid-reply. "Instant" = a single Enter runs it (panel openers only); every other verb completes on the first Enter and runs on the second.

| Verb and arguments | What it does, and what it wraps | Status | Replying |
|---|---|---|---|
| **Chat** | | | |
| `/new` | New chat — `startNewConversation` (`app.js:6530`), the "+ New" button | new typed form | yes |
| `/chat <chat>` (alias `/switch`) | Switch chat — `selectConversation` (`6488`); argument rows are the sidebar list | new | yes |
| `/clear` | Clear this chat — `POST /api/ai/clear`, the Clear button (`4681`); confirms | new typed form | no |
| `/stop` | Stop the running reply (the Stop button) | new typed form | yes |
| `/redo` | Redo the last prompt — `redoAskMessage` (`3016`), the per-message Redo | new typed form | no |
| `/copy [code]` | Copy the last reply, or pick one of its code blocks (I.1) | **new** | yes |
| **Model** | | | |
| `/provider <auto or name[,name…]>` | Provider override for the next asks — `state.providerOverride` (picker, `4700–4790`) | new typed form | yes |
| `/think <off/low/medium/high/show/hide>` | Thinking level and trace visibility — `POST /api/think` | new typed form | yes |
| `/capacity <full/compact/ultra>` | Prompt capacity (400/100/50%) — `POST /api/mode`, the topbar capacity button | new typed form | yes |
| **Skills** | | | |
| `/skillload <skill>` | Force a skill into every reply in this chat — `POST /api/skills/:name/load` | existing (completion rebuilt) | yes |
| `/skillunload <skill or --all>` | Unload; completes from what is loaded | existing (loaded-only completion, `--all`) | yes |
| `/skillmake`, `/skilladd` | Open the Skills manager on the new / import pane | existing | yes |
| `/skills` | Open the Skills manager | new typed form | yes, instant |
| **Run** | | | |
| `/run <saved command> [--var value …]` | Run a saved command — `runSegments` (`2651`), same as Execute in the Commands panel; completes names, then `--var`s with REQUIRED/default hints | new — and the typed way to run one in Focus layout (I-B12) | no |
| `/daemon <start/stop/restart/status> <id>` | `POST /api/daemons/:id/:action`; rows show live state; stop/restart confirm when running | new | yes |
| `/organize-json <path>` | The existing local JSON organiser (bare `organize-json <path>` keeps working) | existing | yes |
| **Panels** — open only | | | |
| `/guides` `/debug` (alias `/tools`) `/checklist` (alias `/tests`) `/schedule` (alias `/sched`) `/mcp` `/ctools` `/channels` `/daemons` `/backlog` `/logsearch` `/setup` | The twelve Menu items (`index.html:197–241`) | new typed forms | yes, instant |
| `/subagents` `/notifications` `/logs` | The topbar Subagents button and the Notifications and Logs corner buttons | new | yes, instant |
| `/config` `/skin` | The Config modal and the Skin modal | new | yes, instant |
| `/layout <classic/focus>` | `POST /api/ui-mode`; the topbar layout switch | new | yes |
| **Utility** | | | |
| `/help [verb]` | The palette itself, usage for one verb, and the key cheat-sheet | new | yes, instant |

**Controls that deliberately get no verb:** the per-message Speak button; the mic (the browser's permission prompt wants a click); the sidebar search box; the sequence builder (Queue / Run sequence — a multi-step UI); the Config sub-forms and Skin colour pickers.

**Focus layout (I-B12).** `body.layout--focus .grid { display: none }` (`style.css:2863`) hides the Commands panel, Menu's twelve items don't include Commands, and REPO_MAP §6 promises that "no feature exists in one layout and not the other". From markup and CSS alone, saved commands are only reachable in Focus by asking the model to run one — worth confirming in the running UI. `/run` closes the gap either way.

### I.2.4 Disposition of every built-in subcommand

Every one of the **146 public subcommand names** — the 116 in `cli.py`'s `RESERVED_NAMES` plus the 30 it doesn't list (I-B2) — gets a disposition here. The table was checked against the repo when this was written (a script asserted it covers exactly that set) and seeds the `covers`/`notExposed` fields of the data file in I.2.7. "Panel only" means the panel owns it, with its own confirmations; "not exposed" means no typed form, for the reason given.

| Family | Names | Disposition |
|---|---|---|
| Skills | `skills-list`, `skills-get`, `skills-save`, `skills-add`, `skills-create`, `skills-remove`, `skillmake`, `skilladd`, `skillload`, `skillunload` | `/skillload`, `/skillunload`, `/skillmake`, `/skilladd`; the `skills-*` names are the manager's REST plumbing — reached through `/skills` |
| Chats | `conv-new`, `conv-list`, `conv-show`, `conv-switch`, `conv-search` | `/new`, `/chat` (chat search is the sidebar box) |
| Chats, not typed | `conv-delete`, `conv-export` | **Not exposed.** Deleting a whole chat by typing a word is a fat-finger hazard (the sidebar confirms); `conv-export` has no web surface |
| Prompt, thinking, layout | `mode`, `mode-set`, `think`, `ui-mode` | `/capacity`, `/think`, `/layout` |
| Chat history | `ai-clear`, `ai-drop-from`, `ai-config` | `ai-clear` → `/clear`; `ai-drop-from` (Redo's plumbing) → `/redo`; `ai-config` → `/config` |
| Config screens | `config`, `playnite-config`, `spotify-config`, `memory-config`, `everything-config`, `voice-config` | `/config` (Config modal). `notify-config`, `mcp-config` and `channels-config` are listed with their panels below |
| Spotify login | `spotify-login` | **Not exposed.** An interactive OAuth flow meant for a terminal |
| Tools | `tools-list`, `tool-run`, `tool-preview`, `tool-safety-set` | `/debug`. `tool-safety-set` is panel only — it changes confirm gating, so never a typed verb |
| Personas | `personas-list` | `/skin` |
| Logs | `logs`, `logs-list`, `logs-show`, `logs-search`, `logs-files`, `logs-tail`, `logs-sets` | `/logs`, `/logsearch` |
| Logs, internal | `logs-append-run`, `logs-clear` | **Not exposed.** `logs-append-run` is the server's own hook; `logs-clear` is a bulk wipe |
| Scheduling | `sched-list`, `sched-add`, `sched-show`, `sched-cancel`, `sched-pause`, `sched-resume`, `sched-snooze`, `sched-approve` | `/schedule` (panel only for the actions) |
| Scheduling, internal | `sched-tick`, `sched-daemon`, `sched-signal`, `sched-ask-log`, `sched-clear` | **Not exposed.** Supervisor and internal hooks; `sched-clear` is a bulk wipe |
| Notifications | `notify-list`, `notify-history`, `notify-ack`, `notify-config` | `/notifications` |
| Notifications, not typed | `notify-send`, `notify-clear` | **Not exposed.** `notify-send` messages every configured channel; `notify-clear` is a bulk wipe |
| MCP | `mcp-status`, `mcp-refresh`, `mcp-tools`, `mcp-config` | `/mcp` |
| MCP, not typed | `mcp-call` | **Not exposed.** Runs an arbitrary MCP tool |
| Channels | `channels-status`, `channels-set`, `channels-test`, `channels-whoami`, `channels-log`, `channels-directory`, `channels-people`, `channels-config` | `/channels` |
| Channels, who may talk to Jarvis | `channels-allow`, `channels-deny`, `channels-follow`, `channels-block` | Panel only — they change who can reach Jarvis, so the panel keeps its own confirmations |
| Daemons | `daemons`, `daemon-start`, `daemon-stop`, `daemon-restart`, `daemon-status` | `/daemons` (panel) and `/daemon <start/stop/restart/status> <id>` |
| Daemons, panel only | `daemon-console`, `daemon-input`, `daemon-schedule`, `daemon-add`, `daemon-edit`, `daemon-remove` | Panel only — AGENTS.md: registering a daemon stores an argv run unattended, so no add/edit from a chat box |
| Daemons, supervisors | `daemon-run`, `daemons-tick`, `discord-daemon`, `instagram-serve`, `clipboard-watch`, `clipboard-watch-config`, `ambient`, `ambient-tick` | **Not exposed.** Processes and supervisors the Daemons registry runs — start and stop them through `/daemon` |
| Backlog | `backlog`, `backlog-add`, `backlog-done`, `backlog-update`, `backlog-remove`, `backlog-board` | `/backlog` (panel; quick-add is a candidate, D-I5) |
| Subagents | `subagents`, `subagent-status`, `subagent-cancel` | `/subagents` |
| Subagents, internal | `subagent-spawn`, `subagent-run`, `subagent-keys` | **Not exposed.** Internal spawn path; `subagent-keys` handles key material |
| Setup | `onboard` | `/setup` |
| Voice | `speak`, `listen`, `transcribe` | **Not exposed.** They sit behind the mic and Speak buttons |
| Local tools | `organize-json` | `/organize-json` (the bare form keeps working) |
| Custom tools | `ctools-list`, `ctools-show`, `ctools-write`, `ctools-check`, `ctools-delete`, `ctools-run`, `ctools-toggle`, `ctools-templates` | `/ctools` (panel) — *one of the 30 unlisted names (I-B2)* |
| Terminal-only or no web surface | `doctor`, `version`, `browser-setup`, `memory-ns`, `memory-list`, `memory-consolidate`, `memory-recall`, `memory-reindex`, `memory-stats`, `calendar-add`, `calendar-list`, `calendar-remove`, `calendar-events`, `digest-status`, `digest-on`, `digest-off`, `digest-now`, `digest-preview`, `policy`, `policy-check`, `policy-dry-run` | **Not exposed.** No web route or panel today (`doctor` and `browser-setup` are terminal tools; the rest are the model's memory / calendar / digest / policy surfaces) — *all unlisted names (I-B2)* |

### I.2.5 Interaction design

**Look and layout.**

- A **floating popover** anchored to the composer — `position: absolute; bottom: 100%` inside a new `position: relative` `.ask-composer` wrapper around the form — at the composer's full width, max-height about 320 px or 50% of the viewport, scrolling inside. Never in flow, so nothing shifts when it opens (I-B6). It sits below modals and the selection pop-up in z-order.
- **Level 1 — verbs**, grouped under small-caps headers (Chat · Model · Skills · Run · Panels · View). A row is: the verb in monospace with the matched letters emphasised, the argument hint dimmed (`<skill>`), a one-line summary, and on the right a badge (`new`, `confirm`, a shortcut).
- **Level 2 — arguments.** A header chip reads `/skillload › pick a skill · 3 of 24`. Rows carry context: a description for skills; a live state dot for daemons; title, relative time and origin badge (Discord / Instagram / Scheduled, via `ORIGIN_LABELS`) for chats; `--var` names with REQUIRED or the default for `/run`.
- **Footer.** Key hints on the left; a `local · no tokens` tag on the right; and a **preview line** saying what Enter will do — "Loads *pdf* for this chat until you unload it."

```
 ┌────────────────────────────────────────────────────────────────────┐
 │ CHAT                                                               │
 │ ▸ /new                  Start a new chat                           │
 │   /chat <chat>          Switch to a saved chat                     │
 │   /clear                Clear this chat                  confirm   │
 │ SKILLS                                                             │
 │   /skillload <skill>    Load a skill into every reply in this chat │
 │   /skillunload <skill>  Unload a skill                             │
 │ ────────────────────────────────────────────────────────────────── │
 │ ↑↓ move · Tab complete · Enter run · Esc close      local · no tokens │
 └────────────────────────────────────────────────────────────────────┘
 ┌ Ask Jarvis anything… · / for commands ─────────────── mic   Send ┐
```

```
 /skillload › pick a skill                                    3 of 24
 ▸ pdf          Read, fill and merge PDF files                 2 refs
   pdf-forms    Fill in PDF form fields
   playnite     Launch and manage games          loaded · this chat
 ──────────────────────────────────────────────────────────────────
 Enter fills "pdf" — Enter again loads it for this chat only
```

**Keyboard.**

| Key | Behaviour |
|---|---|
| ↑ / ↓ | Move the active row (wraps); PgUp/PgDn/Home/End jump; the active row is scrolled into view (`block: "nearest"`) |
| Tab | Complete the highlighted row — a verb becomes `/verb␠`, an argument fills in; **never runs** anything |
| Enter | If the text isn't yet a complete command: same as Tab. If it is: run it. An *instant* verb runs on one Enter. **Never sends a half-typed command** (I-B4) |
| Esc | Close the popover, leave the text alone; a second Esc does what it does today |
| Shift+Enter | A newline as today — and closes the popover, since multi-line text is never a command |
| Enter while `e.isComposing` | Ignored (IME composition) |

Picking a row never runs a command by itself — the same as today's suggestion click, which only fills the input.

**Mouse and touch.** Hover moves the active row (real state, not `:hover`-only CSS); `pointerdown` plus `preventDefault` picks a row without blurring the textarea, which replaces the blur `setTimeout(…, 150)` and `mousedown` workaround (`app.js:5796`, `5810`); rows are at least 40 px tall on touch; scrolling inside the list doesn't close it.

**States.** Loading — the last good list is shown immediately and revalidated behind it, with a skeleton row only on the very first open. Empty — "No command matches `/xyz` — Enter sends it as a message". Error — a row reading "Couldn't load skills — Retry". Disabled rows — an invalid skill (with its reason, I-B7) or a verb that can't run while Jarvis is replying (greyed, with why).

**Accessibility (I-B8).** The combobox pattern: the textarea gets `role="combobox"`, `aria-expanded`, `aria-controls`, `aria-autocomplete="list"` and `aria-activedescendant`; the popover is `role="listbox"`; rows are `role="option"` with ids and `aria-selected`; a polite live region announces "N commands" or "no matches". `prefers-reduced-motion` turns transitions off. Colours come from the existing variables (`--bg-raised`, `--border`, `--accent`, `--text`, `--text-dim`, `--font-mono`) — nothing hard-coded.

**Discoverability.** The placeholder becomes "Ask Jarvis anything… highlight text above to quote it · / for commands", and `/help` lists everything with a key cheat-sheet.

**Matching and ranking.** exact › prefix › word-boundary (after `-`, `_`, space) › substring › subsequence (3+ characters). Ties: recently used first, then registry order. Recency is a small MRU of verb ids and skill/command names in `localStorage` (`jarvis.slash.mru`, capped around 30 — never chat titles). Matched characters are emphasised with DOM nodes, never `innerHTML`.

**Honest feedback (I-B3).** With `/skillunload` completing only from what's actually loaded, a typed name that isn't loaded gets "not loaded" instead of a success toast. The CLI deliberately unloads by whatever name it's given, "even if the skill itself no longer exists" (that's the cleanup case), so the honesty has to come from the front end, which knows the loaded list.

### I.2.6 Grammar and submit semantics

- **Trigger.** The value starts with `/` at column 0 and so far has a single-token first line. Multi-line text is never a command. A first token containing a second `/` (`/home/x`, `/tmp/foo`, `/v1/messages`) is a path, not a command — no popover.
- **Resolution.** **Exact** verb or alias only. A prefix never auto-resolves on Enter — it completes first.
- **Unknown verb (I-B5).** If it's within edit distance 2 of a known verb (and 3+ characters), block the submit and show "Unknown command `/skilload` — did you mean `/skillload`?" with a *Send anyway* action (a second identical Enter sends). Otherwise it's sent as prose — `/etc is where…` stays a normal message (D-I6).
- **Escape hatch.** A leading `//` always sends the text with one slash stripped.
- **Quotes.** Unchanged: while a quote is attached the popover is off and nothing is intercepted.
- **While Jarvis is replying.** Each verb declares it (the inventory's "Replying" column). *Yes*: the skill verbs (today's behaviour — they run before the `state.running` check), `/stop`, `/chat`, `/new` (switching mid-reply is already supported, `app.js:6488`), the panel openers, and `/provider`, `/think`, `/capacity`, `/layout` (they take effect from the next reply, as the skill toast already says). *No*: `/clear`, `/redo`, `/run` (`runSegments` refuses while running anyway).
- **Confirmation.** Destructive or process-affecting verbs use `JarvisUI.confirm` — a real modal whose safe answer is Escape: `/clear`, and `/daemon stop|restart` on a running daemon. (The existing Clear button doesn't confirm — D-I7.)
- **Feedback.** A toast, as today. No new persisted thread item in v1 (D-I8).
- **Compatibility (P8).** The four skill verbs and `organize-json path` behave exactly as before; `/organize-json` is a new alias.

### I.2.7 Architecture and files

- **The registry is data.** `web/public/slash-commands-data.js`: strict JSON between `JSON-BEGIN` / `JSON-END` markers — the same trick `test-checklist-data.js` uses, so a Python test can parse it. Per verb: `id`, `verb`, `aliases`, `group`, `summary`, `args[]` (name, `source`, required), `covers[]` (the CLI names it fronts), `menuItem` (for panel verbs), `instant`, `whileReplying`, `confirm`, `preview`. Plus `notExposed`: `{ "<cli-name>": "<reason>" }`.
- **Engine and UI.** `web/public/slash-palette.js`, loaded after `app.js` like `test-checklist.js`: a pure engine (parse, rank, complete, resolve — testable in Node), the popover UI, and `handlers[id](host, args)`; plus `slash-palette.css`. This follows Part H's convention — own files, not more weight in the 418 KB `app.js` and 125 KB `style.css`.
- **The host facade.** `app.js` sets `window.JarvisHost = { toast, api, state (read-only getters), confirm, copyText, openPanel(id), newChat, selectChat, stop, redo, clearChat, setProvider, setThink, setCapacity, setLayout, runSegments, lastReply }` — thin wrappers over functions that already exist: `startNewConversation` (`6530`), `selectConversation` (`6488`), `openSkills` (`5736`), `openDebug` (`5470`), `openScheduled` (`7407`), `openMcp` (`7499`), `openChannels` (`7631`), `openGuides` (`8175`), `openSubagents` (`8579`), `openDaemons` (`8854`), `openBacklog` (`8991`), `openLogSearch` (`9052`), `openSetup` (`9233`), `runSegments` (`2651`), `Api.clearAiHistory`, the provider picker, and `/api/think`, `/api/mode`, `/api/ui-mode`; Test Checklist opens through `window.JarvisTestChecklist.open()`. None of this needs a new server route.
- **Changes inside `app.js`.** The two regex intercepts in the submit handler become one `JarvisSlash.handleSubmit(text)` call, looked up lazily at submit time so there is no load-order trap. The old suggester goes: `SKILL_SUGGEST_RE`, `updateSkillSuggest`, `hideSkillSuggest`, its two listeners and `skillNamesCache`; `handleSkillSlashCommand` moves into the registry. In `index.html`, `#skill-slash-suggest` is replaced by the popover container inside the new `.ask-composer` wrapper, and the `.skill-slash-suggest*` CSS (`style.css:2491–2520`) is deleted. New script tags go **below** every panel (the "Scripts load LAST" comment in `index.html`).
- **If the palette script fails to load**, the composer must still send ordinary messages: a missing `JarvisSlash` means "not a command".
- **Security posture.** Everything here is typed by the owner into a page bound to `127.0.0.1` (`server.js:27`); nothing gives the model a new capability. `/daemon` has start/stop/restart/status and **no add** — AGENTS.md: registering a daemon "stores an argv Jarvis later runs unattended", so no daemon creation from a chat box. Every string from a skill, chat or daemon is inserted with `textContent` (the `ui-kit.js` / `test-checklist.js` rule): skill descriptions can come from an imported zip and chat titles from a stranger's Discord message.

### I.2.8 Data providers and freshness

| Source | From | Notes |
|---|---|---|
| skills | `GET /api/skills` (exists): name, description, valid, references, version | Invalid skills are shown disabled with the reason (I-B7) |
| loaded skills | **new read:** `jarvis skills-loaded [conv-id]` → `GET /api/skills/loaded?conversationId=` | Wraps `skill_stickiness.get_loaded()`, which exists but nothing exposes. Rows say "this chat" or "all chats" (the global scope). `/skillunload` lists these only, plus an "all loaded" row (`--all` — the CLI already supports `jarvis skillunload --all`; the route would pass it through as the name, worth confirming when built). `/skillload` marks already-loaded skills. The new CLI name joins the canonical reserved set (I-B2) |
| chats | `state.conversations` (the sidebar list, already fetched) | Most recent first, capped around 50, "type to narrow" |
| saved commands | `state.commands` (already loaded from `GET /api/commands`) | Var hints from `spec.vars` — required unless it has a `default` |
| daemons | `GET /api/daemons` | Live state dot |
| providers, levels, modes, layouts | the picker's provider list; static enums (`PROMPT_MODES`, `off/low/medium/high`, `classic/focus`) | |

**Freshness.** Fetch on first open; stale-while-revalidate (show the last good list instantly, refresh behind it); a TTL around 30 s; explicit invalidation wherever the app already knows something changed (`loadSkills()`, `refreshConvoList()`, command create/update/delete, daemon actions). A per-request sequence token — the `voiceRequestSeq` idea near `app.js:4459` — discards a response that arrives after the input has moved on. This replaces today's cache that's nulled after a load/unload (`5830`) and otherwise only refreshed when the manager opens, so a skill added elsewhere never shows up (I-B7).

### I.2.9 Tests and the coverage guard

- **`tests/test_slash_coverage.py`** (Python, in the spirit of `test_checklist_coverage.py`; parses the data file's JSON block). It checks that (1) every name in the canonical reserved set (I-B2's fix) is in some verb's `covers` **or** in `notExposed` with a non-empty reason — adding a subcommand without deciding fails; (2) there are no stale `covers`/`notExposed` names; (3) verbs and aliases are unique and match `^[a-z][a-z0-9-]*$`; (4) every `menu-item-*` id in `index.html` is claimed by exactly one verb; (5) every verb id has a handler in `slash-palette.js`; (6) every verb has a summary and, if it takes arguments, a `source`. Until I-B2's fix lands the test can use `cli.py`'s `RESERVED_NAMES` plus the dispatch scan.
- **`tests/verify_slash_engine.js`** (Node, sliced by marker, plain asserts): the grammar (path vs command, multi-line, `//`), ranking, near-miss detection and argument completion.
- **AGENTS.md** gets a rule beside "Test Checklist": *adding a CLI subcommand or a Menu panel means adding it to the palette data — a verb, or `notExposed` with a reason — in the same change.*
- **Manual browser pass** for what can't run here: I.5.

## I.3 Order of work

> **Current note:** I-B1 and I-B2 are still high-priority pre-work because both are small and independent and were reproduced against the relevant source path. The remainder of this section remains the planned implementation sequence.


Each item is its own patch, per AGENTS.md's one-concept-per-patch rule.

1. `jarvis-math-extract-skips-code.patch` — **I-B1**. Standalone, reproduced, small.
2. `jarvis-reserved-names-single-source.patch` — **I-B2**. Standalone, reproduced; the foundation for the coverage test in step 9.
3. `jarvis-render-pipeline.patch` — `renderRich`, `copyText`; `copyAskRaw` and `#ask-sel-copy` move onto the helper (**I-B9**, **I-B10**). No visible change except the copy fallback. Lands before §8's UI work so streaming has one render entry point.
4. `jarvis-code-blocks.patch` — bar, Copy, collapse, wrap, sticky, user-bubble fences, the delegated handler, speech stripping (**I-B11**), CSS, `verify_code_blocks.js`; then a manual browser pass.
5. `jarvis-code-highlight.patch` — after D-I1.
6. `jarvis-slash-engine.patch` — data file, engine, coverage test; ports the four skill verbs and `organize-json`; deletes the old suggester.
7. `jarvis-slash-palette-ui.patch` — popover, keyboard, accessibility, ranking, states (**I-B4**, **I-B6**, **I-B8**).
8. `jarvis-skills-loaded-route.patch` — the `skills-loaded` CLI subcommand and route (**I-B3**, **I-B7**).
9. `jarvis-slash-verbs.patch` — the rest of the inventory, `/copy` (needs step 4), `/help`, the near-miss handling (**I-B5**), `/run` (**I-B12**).
10. Docs: `REPO_MAP.md` §6, the AGENTS.md rule, `web/README.md`, the placeholder text.

Independent: 1 and 2. Chains: 3 → 4 → 5, and 6 → 7 → 9, with 8 before 9's `/skillunload` completion. Nothing in Part I blocks §8; §8 benefits from step 3.

## I.4 Decisions needed from the owner

- **D-I1 — highlighter source.** CDN, pinned (recommended: it matches marked/DOMPurify/KaTeX and degrades to plain monospace) vs vendored under `web/public/vendor/` (offline-safe, one more vendored file to maintain). I haven't measured the bundle size — do that before choosing.
- **D-I2 — fenced code in your own bubble.** Render it? Recommended yes, fences only.
- **D-I3 — shell prompts on copy.** Strip a leading `$ ` when copying? Recommended yes — only when *every* non-empty line has it and the language is bash/sh/shell/console/zsh, with the tooltip saying so. Otherwise copy is byte-exact.
- **D-I4 — what "every command" means.** Curated verbs plus a disposition for every name (recommended) vs a generic `/jarvis <subcommand>` passthrough for all ~146. Passthrough is not recommended: it hands a typo the bulk wipes (`logs-clear`, `sched-clear`, `notify-clear`), things that message every channel (`notify-send`), the confirm-gating switch (`tool-safety-set`), arbitrary MCP calls (`mcp-call`) and foreground supervisors (`daemon-run`).
- **D-I5 — other new verbs.** What else do you want? Candidates not in v1: `/backlog add <title>` (quick capture), `/remind`, `/persona <name>`, `/search <text>` (chat search), `/export` (`conv-export` has no web surface today).
- **D-I6 — near-miss typos.** Block with a hint (recommended) vs always send to the model.
- **D-I7 — confirmations.** `/clear` and stopping/restarting a running daemon confirm (recommended). Should the existing **Clear** button confirm too? It doesn't today (`app.js:4681`), while deleting a chat does (native `confirm()` in `deleteConversationConfirm`).
- **D-I8 — command feedback.** Toast only (recommended) vs a persisted "command chip" in the thread — a persisted item interacts with Part E's per-exchange bucketing (`exchangeCountByConv`).
- **D-I9 — existing name collisions (I-B2).** Warn in `jarvis doctor` (recommended) vs auto-rename (not recommended — it edits your `commands.json`).

## I.5 Acceptance checklist

**Code blocks**

- [ ] A fence renders as a block with a language label and line count; `~~~` fences too.
- [ ] One click on Copy puts exactly the source on the clipboard; the button reads "Copied" and reverts; no toast.
- [ ] Select-all inside a block, Ctrl+C, paste → identical to the source (no label, no "Copy", no line numbers).
- [ ] With `navigator.clipboard` unavailable or denied, Copy still works through the fallback — and so do the message Copy and the selection pop-up.
- [ ] A collapsed block's Copy copies every line and says how many.
- [ ] A reply with `$$`, `\[…\]` or two `$` inside code renders exactly as written (I-B1 cases A–D); math outside code still typesets.
- [ ] A live reply and the same reply after a reload or chat switch look identical.
- [ ] A forged `<button class="codeblock__copy">` in model text does nothing.
- [ ] Highlighter blocked → plain monospace, Copy still works.
- [ ] Highlight colours are legible in every skin and persona theme.
- [ ] An unclosed fence mid-reply: the bar says "writing…", Copy is disabled until it closes, and nothing after it turns into code.
- [ ] Speak and the mic speak-back skip code.
- [ ] Your own pasted fence renders (if D-I2 = yes).

**Palette**

- [ ] `/` opens the palette; every verb is listed with a description; typing narrows it; matches are emphasised.
- [ ] ↑/↓/Tab/Enter/Esc behave as specified; Enter never sends a half-typed command; IME composition is safe.
- [ ] The popover floats — the thread doesn't move when it opens.
- [ ] `/skillload ` lists skills with descriptions, invalid ones disabled with a reason, already-loaded ones marked; `/skillunload ` lists only loaded skills plus "all".
- [ ] `/skillunload typo` no longer reports success (I-B3).
- [ ] `/skilload pdf` is caught with a hint, not sent to the model (I-B5); `/etc/hosts …` is sent as a normal message; `//new` sends "/new".
- [ ] The four skill verbs and `organize-json path` behave as before.
- [ ] Every Menu panel opens from its verb; `/run`, `/daemon`, `/provider`, `/think`, `/capacity`, `/layout`, `/copy` and `/help` work; a verb that can't run mid-reply says why.
- [ ] `/clear` and daemon stop/restart confirm.
- [ ] A screen reader announces the list, the active row and the result count.
- [ ] Same behaviour in Classic and Focus layouts, and at phone width.
- [ ] `tests/test_slash_coverage.py` and `tests/test_reserved_names.py` pass — and fail when a subcommand or panel is added without a decision.
- [ ] With the palette script blocked or failing, the composer still sends messages.

**Not verified until someone runs it in a real browser:** selection-copy and clipboard permissions, DOMPurify on the new markup, highlight.js and its token colours per skin, sticky and collapse behaviour, streaming, screen-reader output, phone width, Focus layout.

---

# Part J — Final step: a checklist entry for every tool, and a test for every feature, this plan adds

> **CURRENT AUDIT — 2026-09-22:** J remains the final QA closure pass. Tool coverage exists, but this Part should not be marked complete until the missing fixtures are restored, the P0/P1 defects are closed, open features have real acceptance runs, and the 16 plan-added tool entries have been human-tightened.


**Status: not started — this Part itself is new (revision 2026-09-21e).** It
is not a feature to build; it's the closing pass over everything Parts A–I
add or change, so nothing this plan shipped is left untested or undocumented
in Menu → Test Checklist or in this doc's own acceptance checklists.

## J.0 Why this is last, on purpose

Every item below needs the thing it's testing to actually exist and have
settled behaviour — a `does`/`steps`/`expect` entry written against a feature
still in flux just gets rewritten the moment the feature changes, which is
wasted work and (per Part G's own rule) un-ticks every tester's saved
progress since ticks are keyed by step text. So: don't start this early.
Item 12 in §0.4 points here. This also depends on **G.1** (the per-tool
`TEST_CHECKLIST` mechanism) already being delivered, which it is.

## J.1 Two kinds of coverage, two different homes

This plan adds things in two shapes, and they don't fit the same box:

- **Real tools** — something the model can call. These already have a home:
  Menu → Test Checklist, via the `does`/`steps`/`expect` shape G.1 formalized.
  §J.2 below is the inventory.
- **Everything else** — a behaviour change, a UI panel, a backend policy.
  Not a tool, so it can't get a `TEST_CHECKLIST` entry (that catalogue is
  keyed by tool name) — forcing a fake tool name onto "the router no longer
  mis-routes a pasted reply" would just be noise in a tool tester's list.
  This plan's own convention is already the right home for these: a
  per-Part **Acceptance checklist** section. §8.8, E.7, F.18 and I.5 already
  exist; §J.3 below points at those and drafts the ones that don't exist yet
  (Parts B, C, D, G, H).

## J.2 Tool checklist — the 16 tools this plan adds

All 16 already have a first-draft entry in `test-checklist-data.js` (see
Part G, "State after the merge") — this is the human pass those drafts were
always waiting on, plus the live-model prompt to run each with. Tightening
these can use either home G.1 offers (the shipped file, or moving an entry
into the tool's own module via `TEST_CHECKLIST`) — migration is optional,
per G.1's own "Not done/open" note; it only matters if someone wants the
entry to live next to the code.

| Tool | Group | Ask prompt to test with | What a pass looks like |
|---|---|---|---|
| `clipboard_get` | clipboard | "What's on my clipboard right now?" | Returns the current text (or a clear "nothing to return"); doesn't restate a copied password verbatim (Part B's safety note) |
| `clipboard_set` | clipboard | "Copy this to my clipboard: hello world" | Clipboard contains exactly `hello world`; paste manually to confirm |
| `clipboard_clear` | clipboard | "Clear my clipboard" | Clipboard is empty afterward |
| `clipboard_wait_for_change` | clipboard | "Watch my clipboard and tell me when I copy something new" — then copy something within ~20s | Returns the new text once it changes; returns `timed_out: true` if nothing changes before the cap (~120s) |
| `browser_goto` | browser | "Open example.com in the browser" | Session starts (or reuses this ask's session) and navigates; a `file://` or `javascript:` target is refused or confirmed, not followed silently |
| `browser_click` | browser | "Click the Submit button on this page" | Confirm-gated (`tool_safety.py`); resolves the element by role/label/text before falling back to a raw selector |
| `browser_fill` | browser | "Type my email into the email field" | Confirm-gated; fills the described field, not a guessed CSS target |
| `browser_get_text` | browser | "What does this page say?" | Returns visible page text, truncated the same way `web_fetch` is |
| `browser_screenshot` | browser | "Take a screenshot of this page" | Saved file path returned and displayed the same way the desktop-screenshot tool's output is |
| `browser_wait_for` | browser | Inside a multi-step flow: "Wait for the results to load, then tell me what's there" | Waits up to `timeout_seconds` for the described element/text before failing, instead of reading a half-loaded page |
| `browser_close` | browser | "Close the browser now" | Session ends before the ask itself would have closed it |
| `move_path` | files | "Move report.pdf to my Desktop" | File moved; confirm-gated; refuses drive roots / home folder / OS folders / `~/.jarvis` outright |
| `copy_path` | files | "Copy report.pdf to the Desktop, don't move it" | Original stays in place, a copy appears; won't overwrite unless told to |
| `rename_path` | files | "Rename draft.txt to final.txt" | File renamed in place |
| `make_dir` | files | "Create a folder called Invoices inside Documents" | Folder created; parent folders only created if asked |
| `delete_path` | files | "Delete old-notes.txt" | Goes to the Recycle Bin, not permanently unlinked; confirm-gated |

## J.3 Feature checklist — everything else this plan adds

Grouped by Part. Where a Part already has its own Acceptance checklist, this
just points there instead of duplicating it — and, for the ones that are
automated-test-only today, adds the live-model prompt those sections
themselves flag as still missing. Where a Part has no acceptance checklist
yet, one is drafted here.

**Part A** — §8 already has its own acceptance checklist (§8.8); use that
directly once streaming exists. The rest of Part A:

| Item | Ask prompt / manual step | Expect |
|---|---|---|
| §2 — OCR/Tesseract diagnosis | Run OCR on an image with Tesseract deliberately off PATH | Error names the real cause (missing PATH entry), not a generic OCR failure |
| §3 — Debug-menu source filter | *(held off at the owner's instruction — don't test until it's actually built)* | — |
| §4 — Ollama thinking mode | Ask something via Ollama with `/think` on | Thinking shows/hides per the current `/think` setting |
| §5 — interim text alongside tool calls (Anthropic only today) | Ask something where the model talks, then calls a tool | The in-between text survives into the reply on Anthropic; the same prompt on Gemini/OpenAI-compatible/Cohere/Ollama is *expected* to still drop it — that's the tracked gap, not a new bug |
| §6 — thinking between tool calls (all providers) | A multi-tool-call turn with thinking on | A thinking block appears between each tool call, not only once at the start |
| §7 — `routed: x` for auto-discovered tools | Call an auto-discovered tool, check Debug/log output | Shows `routed: <group>` like any core tool |
| §1g — 100k+ input-token burn containment | Replay the supplied `ee8560f14376332f.jsonl` (or a redacted equivalent with the same repeated-turn shape) | Reported input-token total is lower than the 123,368-token baseline; unchanged history is no longer recopied indefinitely; repeated tool discovery/cancellation paths do not multiply the same context without new information |

**Part B** — the four core clipboard tools are covered in §J.2. The
Continuous-watch feature is not a tool (item 3 of that section was
deliberately not built) and needs its own row:

| Item | Ask prompt / manual step | Expect |
|---|---|---|
| Clipboard-watch daemon | "Turn on clipboard watching for anything matching an email address" (drives `daemon_start` on the built-in `clipboard-watch` daemon) | Daemon appears running in the Daemons panel; a notification fires on a matching copy; `daemon_stop`/"turn it off" stops it |
| Clipboard notification flood control | Generate a burst of many matching clipboard changes in a few seconds, then one isolated change | Burst produces a bounded/coalesced notification count with a truthful event count; the isolated change still arrives promptly; the durable inbox still contains the individual events |

**Part C** — the 7 browser tools are covered in §J.2. The rest are not tools:

| Item | Ask prompt / manual step | Expect |
|---|---|---|
| `jarvis browser-setup` | Run it on a machine without Playwright installed | Reports pass/fail per install step, same shape as `doctor.py` |
| Confirm-gating on `browser_click`/`browser_fill` | Ask it to click/fill something that navigates off-origin or matches the blocklist (buy, purchase, pay, delete, remove, confirm, submit, transfer) | Confirmation is requested before the action runs |
| `browser_warm_daemon` off (default) | Any `browser_*` call on a fresh checkout | Behaves exactly like v1 — no daemon process appears |
| `browser_warm_daemon` on | Turn the flag on, run two `jarvis ask` calls back to back that each use `browser_*` | Second ask reuses the already-warm daemon (visibly faster than the first's Chromium startup); daemon shows running via `jarvis daemon-status browser-daemon` |
| Daemon idle-timeout | Leave the daemon idle past `browser_daemon_idle_seconds` | Browser closes but the daemon keeps listening; the next `browser_*` call reopens it, paying the startup cost once |
| Daemon unreachable/disabled fallback | Stop the daemon, then make a `browser_*` call with the flag still on | Falls back to the v1 per-ask session transparently, no error surfaced to the user |

**Part D** — no acceptance checklist exists yet; drafted here:

| Item | Ask prompt / manual step | Expect |
|---|---|---|
| D.1 — subagent live view (near-live tier) | Start a subagent task, watch the panel while it runs | Tool calls/console/`thinking` extras appear roughly every ~1s, not only after the task finishes via the transcript link |
| D.1 — subagent live view (word-by-word tier) | Same, once §8 streaming exists | Text streams the same way the main chat does |
| D.2 — notification summaries | Trigger a scheduled ask/command notification | A short digest (first line / ~200 chars, expandable), not raw `JARVIS_USAGE`/`JARVIS_CONFIRM_REQUEST` JSON — checked across every notification source, not just the two scheduler paths |
| D.2.1 — notification importance levels | Trigger one `high`, one `normal`, and one `low` notification, including a clipboard-watch notification | Resolved priority is stored and visible; `high` is immediate and not batched, `low` enters the existing digest when enabled, and `normal` is immediate subject only to burst coalescing |
| D.2.1 — explicit override vs kind default | Create a notification whose kind normally maps to `low`, then explicitly set `high` | Explicit importance wins over the kind default and the delivery follows `high` semantics |
| D.3 — scheduler prompt/logging items | Run a scheduled job, check Log search for the prompt; check token cost of an unrelated turn | Prompt appears in Log search for every job type; scheduler guidance no longer costs tokens on turns that don't use it |

**Part E** — already has its own acceptance checklist (E.7); use that
directly once built.

**Part F** — already has its own acceptance checklist (F.18), but it's
automated-tests-only in several places ("not yet seen end-to-end with a live
model", "not run on a real Windows box"). This plan's contribution is the
live-model prompt for those specific gaps:

| Item | Ask prompt / manual step | Expect |
|---|---|---|
| F.1/F.8/F.11 — forced ending | Exhaust the tool-round budget mid-task with a real model | A harness-written degraded reply summarizing what ran and what's left — no key rotation, no "empty response" |
| F.2 — unknown-tool hint | Ask something that makes the model try a plausible but nonexistent tool name | A `did_you_mean`/`search_tools` hint, not silent failure |
| F.4 — `run_shell` on Windows | On a real Windows box: `python -c "print(1)"` via `run_shell` | Runs and returns real output instead of silently doing nothing |
| F.5 — `code_agent` sees `.env` | Ask `code_agent` to read or edit a value in `.env` | It can see and use the file; no tool returns a `.env` value by default |
| F.6 — `code_agent` result shaping | Give `code_agent` a small, real fix task | Result's `log` field shows short outcome lines per step, not bare tool names or a raw `steps` dump |
| F.7 — failover transcript | Force a provider failure mid-turn (real key exhaustion or a stubbed failure) | The next provider gets the real prior transcript, not a truncated recap in one fake "user" message |
| F.9 — key health | Trigger a real 429/503 from a key | That key/model cools down and isn't retried immediately; last-good key tried first afterward |
| F.10 — router paste-confirmation | Paste Jarvis's own previous reply back, then send a short confirmation ("yes plz run this") | Doesn't mis-route off the pasted text; the confirmation merges into the still-live sticky group |

**Part G** — the checklist panel testing itself:

| Item | Manual step | Expect |
|---|---|---|
| Panel persistence | Set a verdict, tick a step, reload the page | Verdict/ticks persist (localStorage-backed) |
| Export/Import | Export, clear, Import | Round-trips identically |
| Coverage | Add a tool with no entry anywhere | Shows bare-name **NO CHECKLIST**, listed in Overview's Coverage section |
| Module-supplied entries | Add a custom tool with its own `TEST_CHECKLIST` | Entry appears with the "From the tool's own file" badge; a malformed entry is dropped-and-logged, not a crash |

**Part H** — no acceptance checklist exists yet (Part H is not started);
drafted here for when it lands:

| Item | Manual step | Expect |
|---|---|---|
| H.1 — Daemons rework | Open Daemons panel | Per-row live-state dot, a console that separates stdout/stderr/crash traceback, keyboard shortcuts, a crash-loop indicator |
| H.1 — Backlog rework | Open Backlog panel | Overview-pane counts per column, a "blocked" roll-up, project filter, live search |
| H.1 — Log search rework | Search a known log line | Query term highlighted in the hit; results filter live as you type; a hit links into the relevant Daemons console or Ask conversation |
| H.2 — Schedules pass | Open Schedules panel with jobs of different kinds | Grouped/filterable by kind; next-run time and recurrence shown per row; paused/failed jobs visually distinct |
| H.3 — creation notification | "Remind me tomorrow at 9am to call the dentist" | A confirmation notification fires immediately ("Reminder set for tomorrow 9am: call the dentist"), separate from the reminder itself firing later; `tool_notify_me`'s no-`when` branch does **not** get a duplicate confirmation |

**Part I** — already has its own acceptance checklist (I.5); use that
directly once built.

## J.4 Complex, multi-step scenarios

§J.2 and §J.3 are smoke tests — one prompt, one expected shape. That's
enough for most of what this plan adds, but not for the handful of tools and
fixes where the actual risk only shows up across several steps, a second
`jarvis` process, or a specific historical failure case. Run these in
addition to §J.2/§J.3, not instead of them, for the items below.

**Browser control (Part C) — the most stateful thing this plan adds**
1. Full session lifecycle in one ask: navigate, log in, navigate to a second
   page on the same site, read it. Expect one session opened lazily on the
   first `browser_*` call, reused for every later `browser_*` call in that
   same ask, and closed in a `finally` when the ask ends — test all three
   ends (success, error, timeout), not just the happy path.
2. **Persistent login across asks** — log in during ask 1, let that ask end,
   start a brand-new `jarvis ask` and open a page that requires login.
   Expect still logged in, via the on-disk profile, even though ask 1's
   browser *process* is gone. This is the one thing Part C itself flags as
   unverified without a real environment — don't skip it.
3. Selector fallback: click something described only in prose on a page
   where the accessible role/label/text differ from any CSS class name.
   Expect `get_by_role` → `get_by_label` → `get_by_text` tried in order
   before any raw-CSS fallback.
4. Safety refusals, each as its own case: a `javascript:` URL, a `file://`
   URL, a click whose accessible name matches the confirm blocklist
   (buy/purchase/pay/delete/remove/confirm/submit/transfer), and a click
   that navigates to a new origin. Expect all four to refuse or confirm; a
   same-origin, non-blocklisted click should *not* need confirmation.
5. Playwright not installed: run any `browser_*` tool. Expect the "Browser
   control isn't set up yet. Run: jarvis browser-setup" error, not an import
   crash that takes the rest of `tools.py` down.
6. **Warm daemon (v2), `browser_warm_daemon` on** — repeat scenario 2
   (persistent login across separate `jarvis ask` invocations) with the flag
   on. Expect the same login persistence, now via the daemon's one held-open
   session rather than the on-disk profile alone, and each ask after the
   first noticeably skipping the ~1-2s Chromium startup cost. Then stop the
   daemon process and repeat a `browser_*` call with the flag still on —
   expect a transparent fall-back to v1 behaviour, not an error.

**`clipboard_wait_for_change` (Part B)**
1. Copy something new partway through the ask. Expect the *tool call*
   itself blocks (not a background asyncio task), polling ~0.5s, returning
   the new text as soon as it changes.
2. Let the timeout elapse untouched. Expect `{"ok": false, "timed_out": true}`,
   not a hang.
3. Ask for a timeout above the ~120s cap. Expect it clamped, not honoured.

**100k+ token-burn containment (§1g)**
1. Replay the supplied JSONL in a redacted/stubbed harness and record the same
baseline metrics: 77 requests, 70 usage records, 123,368 input tokens, 2,027
output tokens, and the repeated `write_on_screen continue and enter` history
shape.
2. Compare the new run against the baseline. Expect input-token growth to
flatten once the same user/tool state stops changing, rather than increasing
just because the turn continues.
3. Verify that explicit cancellation/decline terminates the repeated action loop
and that a later, genuinely new request still starts cleanly.

**Notification importance + clipboard flood control (D.2.1 / Part B)**
1. Create one notification at each importance level and verify the persisted
priority, channel behavior and digest behavior.
2. Run a clipboard watcher through a synthetic burst (for example 20 matching
changes) and then one isolated change. Expect bounded push notifications, a
coalesced count for the burst, and the isolated change delivered promptly.
3. Mark the same burst as `high` and confirm the high-importance path is not
silently suppressed by the coalescer.

**Path tools (F.3)**
1. Four separate refusal cases, not one: a drive root, the home folder, an
   OS folder, and something under `~/.jarvis`.
2. `copy_path` onto an existing destination without `overwrite`, then again
   with it — refused, then succeeds, and only ever replaces a *file*.
3. A cross-drive/cross-volume `move_path` (skip cleanly if no second volume
   is available rather than failing the run).

**Forced ending / withheld tools (F.1/F.8/F.11)**
1. Replay Case 1 and Case 2b from F.17's saved-log fixtures end to end, not
   just through the router. Case 1 should end in an edit; Case 2b is a known
   open gap — confirm it still fails, and fails the *same* way, not a new one.
2. Drive a real conversation into a tools-withheld round containing a
   correct tool call. Expect the call is kept, not discarded, and the key is
   not rotated.
3. Trigger each of F.8's four distinct failure shapes separately. Expect all
   four now produce the same harness-written degraded reply, not four
   different flavours of "empty response."
4. Run a turn all the way out of rounds. Expect a reply listing what ran and
   what's left — no key rotation, no "tool budget" wording leaking into the
   model-facing text.
5. Force a real ending that offers one call, then reply with a bare "go
   ahead" as the very next message. Expect the call runs directly with
   **zero** new provider calls, still through `tool_safety.py`'s confirm
   gate. Then check the edges: a non-confirmation reply after the offer
   (expect nothing runs), the same "go ahead" from a chat guest (expect
   ignored), waiting past 30 minutes before confirming (expect expired,
   ignored), and confirming after the offered tool has been removed from the
   session's schemas (expect ignored, not a crash).
6. Immediately after a forced ending, start a **new, unrelated** turn instead
   of confirming. Expect the stale pending action is not carried into it and
   doesn't fire on some later unrelated "yes."

**Failover (F.7)**
1. Get several tool calls into a turn, then force the active provider to
   fail. Expect the next provider's first message carries the real prior
   tool results (not a truncated recap in one fake "user" turn), and nothing
   gets called a second time just because the transcript was rebuilt.

**Key health (F.9)**
1. Trigger a real 429 on a key, then immediately start a new `jarvis`
   process. Expect that key skipped until its cooldown passes.
2. Run again after the cooldown passes. Expect the previously-bad key
   eligible again, with a never-failed key still preferred first.

**Router paste/confirmation (F.10)**
1. Paste Jarvis's own full previous reply back as the entire next message.
   Expect no change to which tool group loads.
2. After a `files`-group turn, send a bare "yes". Expect it still routes
   `files`, not a reset to some default group.
3. Highlight and send only an excerpt of a previous reply. Expect the
   excerpt doesn't vote in the router the way a fresh message would.

**Streaming, once §8 exists**
1. Run the same non-trivial multi-tool-call task on all five adapters
   (Ollama, openai_compatible, Anthropic, Gemini, Cohere) back to back.
   Expect text and thinking both streaming token-by-token on every one, with
   an identical segmented thinking → text → tool UI across adapters.
2. Kill the connection mid-stream on each adapter. Expect the same clean
   degraded behaviour everywhere, not just on whichever adapter §8 was built
   against first.

**Subagent live view (D.1)**
1. Spawn two or more subagents at once, cycle between them mid-run. Expect
   each shows its own live tool calls/console/thinking — never another
   subagent's — and cycling doesn't lose or blend state.
2. Once §8 lands, repeat, confirming the word-by-word tier now streams
   instead of updating once per completed round.

**Console persistence (Part E)**
1. Reproduce the exact scenario behind the saved conversation
   `205b1493df955d1c.json` that Part E's own evidence section is built from.
   Expect it now persists and filters correctly — this is the one area the
   plan explicitly warns has failed before, so a general "looks fine" pass
   doesn't count; re-run the specific case that broke it previously.

## J.5 Where this actually lives

- **Tool entries** (§J.2): tightened in place in `test-checklist-data.js` —
  no migration into per-module `TEST_CHECKLIST` files required, though G.1
  supports it if a maintainer later wants an entry to live next to its tool's
  code.
- **Feature entries** (§J.3) and **scenarios** (§J.4): this section is the
  consolidated home for both. Each Part's own Acceptance checklist heading
  (§8.8/E.7/F.18/I.5, plus the four drafted here for B/C/D/G/H) stays the
  source of truth; §J.3/§J.4 exist so there's one place to read top to
  bottom instead of hunting through nine Parts.
- **Optional follow-up, not required for this step:** a small coverage test
  in the spirit of `test_checklist_coverage.py` that fails when a Part marked
  **Delivered** has no Acceptance-checklist section — worth considering once
  Part J itself is done, not before.

## J.6 Acceptance checklist for Part J

- [ ] All 16 tools from §J.2 have a tightened, human-reviewed entry — no
      first-draft text left.
- [ ] Nothing this plan added shows **NO CHECKLIST** in Menu → Test Checklist.
- [ ] Every Part whose status is Delivered has an Acceptance-checklist section
      in this doc (existing: §8.8, E.7, F.18, I.5; drafted here: B, C, D, G, H).
- [ ] Every row in §J.3, and every scenario in §J.4, has been run at least
      once against a real model/UI, not just reasoned about.
- [ ] The supplied 100k+ token-burn replay has a recorded before/after token
      measurement and no regression in the final action semantics.
- [ ] Notification levels and clipboard flood control have both been exercised
      with burst cases and explicit high/low-priority cases.
- [ ] A tester unfamiliar with this plan can run §J.2 and §J.3 top to bottom
      and reach a verdict on each row without opening the source.


# Part K — Current 2026-09-22 audit: what exists, what is missing, priorities and exact next steps

> **Purpose:** This is the actionable part of the document. It reconciles the
> historical plan with the actual `jarvis-main(47).zip` archive and turns the
> remaining work into an explicit hierarchy. Use the format **big step →
> substep → smaller step → status → what it does → priority → dependency /
> acceptance** when implementing.
>
> **Source of truth:** current source tree first; current tests and executable
> build metadata second; historical revision prose only for provenance.

## K.0 Repo-wide snapshot

The audited archive contains approximately **206 Python files, 7 JavaScript
files, 3 CSS files, 25 Markdown files, and 68 standalone `tests/test_*.py`
scripts**. The current repository map describes three front ends (CLI, web UI,
Discord/Instagram gateways) feeding `ai_client.ask()`, with router, prompt
assembly, provider adapters and the shared tool executor underneath.

The current tool catalogue reports **170 tools**, and the shipped Test Checklist
catalogue also has **170 entries**, with **0 missing names and 0 extra names** in
the audit. The current static tool registry contains the expected groups for
core, commands, files/workspace, web/audio/vision, scheduling, channels,
memory, MCP, subagents, browser, clipboard and other product-specific tools.
The existing notification stack already contains a digest priority model
(`low` / `normal` / `high`), but the priority is not yet first-class on
`notifier.notify()` records; the new revision treats that as the foundation
for end-to-end importance and clipboard flood control.

Existing out-of-plan subsystems were also checked — memory/history,
conversation search and summarization, scheduler/tasks, daemons, backlog,
ambient monitoring, onboarding, Discord/Instagram, Playnite, Spotify,
Everything, yt-dlp, desktop/file/git/package/web/audio/radio tooling and the
custom-tools ecosystem. They are **existing product surface, not silently
converted into “missing” work**. This plan is specifically the backlog for the
issues/features captured in Parts A–J plus the cross-cutting defects found by
the current audit.

## K.1 P0 — release/security gates

### K.1.1 Subagent provider-pool fail-closed correctness

**Status:** OPEN — reproducible today.

**What it does:** A subagent process must be able to use only the provider/key
pool assigned to it. A malformed `JARVIS_SUBAGENT_KEYS` value must result in
**zero eligible providers**, never the parent Jarvis provider list and never an
uncaught `NoneType` iteration crash.

**Current source path:** `jarvis-cli/jarvis/subagents.py`
`providers_from_env()` + `jarvis-cli/jarvis/ai_client.py`
`_eligible_providers()`.

**Current defect:** malformed JSON and non-dict JSON return `None` from
`providers_from_env()`. `_eligible_providers()` correctly intends to “fail
closed” once it knows the process is a subagent, but the parser still hands it
`None` for those malformed shapes. The current regression test
`tests/test_subagents.py::test_malformed_pool_yields_nothing_not_main_key`
therefore crashes with `TypeError: 'NoneType' object is not iterable`.

#### K.1.1.1 Fix parser return contract

**Status:** OPEN.

**Change:** When `JARVIS_SUBAGENT_KEYS` is set but malformed (invalid JSON,
non-object JSON, unusable provider name/key list), return `[]`, not `None`.
Reserve `None` strictly for “this is not a subagent process”.

**Priority:** P0.

**Acceptance:** malformed pool shapes all return `[]`; a subagent never falls
through to ambient providers; the existing test passes.

#### K.1.1.2 Add shape matrix regression coverage

**Status:** OPEN.

**Change:** Cover invalid JSON, JSON scalar/list, missing provider, empty key
list, unknown provider, valid pool, and unexpected exception in lookup.

**Priority:** P0.

**Acceptance:** all cases fail closed and never include a parent/main key.

### K.1.2 Build/source-hash integrity

**Status:** OPEN — directly measured in the current archive.

**What it does:** Makes the build metadata prove which source tree produced the
current executable/package.

**Evidence:**
- `jarvis-cli/jarvis/build_info.py`: build 26, timestamp `2026-09-22 18:05:10`,
  source hash `62765d15d37183066ac4a2e35122111765d14541cfdd62a725665ffe2dee87b9`.
- The repo's own `build_tools.hash_source.hash_source_tree()` on the checked-in
  source computes `4fac34eb84638c653e3af2cde7cce8a9f786512118ad32d2ca94cc59c5b209d0`.

#### K.1.2.1 Regenerate build metadata

**Status:** OPEN.

**Change:** Run the official build metadata path against the actual source tree
so `SOURCE_HASH` equals the computed tree hash.

**Priority:** P0.

#### K.1.2.2 Rebuild and verify the executable

**Status:** OPEN.

**Change:** Build the current `jarvis` executable/package from the same tree and
run the repo's executable verification path so the installed executable and
source metadata agree.

**Priority:** P0.

#### K.1.2.3 Prevent future drift automatically

**Status:** OPEN.

**Change:** Add a release/CI/build check that recomputes the source hash and
fails the build when `build_info.py` is stale.

**Priority:** P1.

## K.2 P1 — correctness and regression blockers

### K.2.1 Restore F.17 real-log replay fixtures

**Status:** OPEN / missing from current archive.

**What it does:** Replays the two real failure scenarios that motivated Part F,
so the most important model/tool-loop fixes are tested against realistic
provider transcripts instead of only canned unit responses.

**Current evidence:** `tests/test_replay_fixtures.py` expects two fixture pairs
under `tests/fixtures/`, but the directory is absent. Current result is
`0 passed, 0 failed, 2 skipped`.

#### K.2.1.1 Restore the four fixture files

**Status:** OPEN.

**Change:** Re-add the two `.jsonl` request/response fixtures and two expected
result `.json` files referenced by the harness, or recapture equivalent
redaction-safe fixtures from the original logs.

**Priority:** P1.

#### K.2.1.2 Re-run Case 1 and Case 2b

**Status:** OPEN until fixtures are restored.

**Acceptance:** Case 1 routing replay passes; Case 2b must exercise the current
`search_files` → `move_path` budget path and retain its known-gap result until
that gap is actually fixed.

**Priority:** P1.

### K.2.2 Fix I-B1 — math extraction must skip Markdown code

**Status:** OPEN — reproduced against current code.

**What it does:** Keeps `$`, `$$`, `\(` / `\)`, and `\[` / `\]` math parsing from
consuming fenced or inline code.

**Current source:** `web/public/app.js::extractMath()` runs regex replacement on
raw reply text before Markdown parsing. It therefore cannot know whether a
match is inside a code fence or inline-code span.

#### K.2.2.1 Build a code-aware extraction pass

**Status:** OPEN.

**Change:** Scan Markdown into code/non-code spans first, or otherwise protect
code fences/inline code before math extraction. Apply the minimum change that
keeps current KaTeX behavior for normal prose.

**Priority:** P1.

#### K.2.2.2 Add regression cases for fenced and inline code

**Status:** OPEN.

**Change:** Extend `tests/verify_math_rendering.js` or add a dedicated test with
`$$` inside fenced code, dollar pairs inside inline code, and `\[`/`\]` inside
code.

**Priority:** P1.

#### K.2.2.3 Run a real browser render check

**Status:** OPEN / environment dependent.

**Acceptance:** code fences remain byte-faithful and math still renders in a
real browser.

**Priority:** P2 after the source fix.

### K.2.3 Fix I-B2 — one canonical reserved-command-name source

**Status:** OPEN — reproduced against current code structure.

**What it does:** Prevents a saved command from being created under a name that
the CLI/web later treats as a built-in, and prevents the three surfaces from
silently disagreeing about what is reserved.

**Current source:**
- `jarvis-cli/cli.py` has the large CLI reserved-name set.
- `web/server.js` has a separate web reserved-name set.
- `jarvis-cli/jarvis/commands_config.py` has a third set.

**Current audit:** the sets are materially different; `think` is a concrete
example of a built-in that can be accepted by a narrower validator and then
shadowed at dispatch, and dozens of public built-ins do not appear in every
list.

#### K.2.3.1 Create one authoritative reserved-name provider

**Status:** OPEN.

**Change:** Put the canonical list in one importable/shared source or expose it
through a single CLI/API definition rather than maintaining three copies.

**Priority:** P1.

#### K.2.3.2 Make web validation consume the canonical list

**Status:** OPEN.

**Priority:** P1.

#### K.2.3.3 Add exact coverage + collision tests

**Status:** OPEN.

**Acceptance:** `think` and every built-in are rejected for saved-command names;
no built-in is missing from the canonical set; a future built-in cannot be
added without updating the same source.

**Priority:** P1.

### K.2.4 Finish F.2 project-content discovery budgeting

**Status:** PARTIAL / owner-policy follow-on.

**What it does:** Stops “I need to inspect the repo to discover the right tool”
from consuming the same scarce round budget as the actual mutation once the
model already found the tool it needs.

**Current source:** `ai_providers.py` has a separate discovery budget for
catalog-style discovery (`search_tools`, `get_tool_schema`, `load_skill`, plus
unknown-name recovery). Current default is 3 and the regression suite passes.

**Current gap:** `search_files` and similar project-content lookup are treated
as ordinary work. Case 2b can therefore burn the round budget searching files
and still fail to perform the eventual `move_path`.

#### K.2.4.1 Decide the policy boundary

**Status:** OPEN.

**Decision:** Should project-content discovery (`search_files`, possibly
`list_dir` and a tightly bounded subset of read-only repo-inspection tools) have
its own discovery budget, or remain billable work?

**Priority:** P1.

#### K.2.4.2 Implement only the chosen boundary

**Status:** BLOCKED on K.2.4.1.

**Acceptance:** Case 2b fixture demonstrates that discovery no longer crowds out
an otherwise budgetable action.

**Priority:** P1.

### K.2.5 Finish Part E Ask-console persistence/replay

**Status:** PARTIAL / OPEN.

**What it does:** Makes the Ask panel's right-side command/console trace survive
reload, reconnect, Stop, process interruption and conversation switching in
the same way the dedicated console store already survives for live runs.

**Current source:**
- `jarvis-cli/jarvis/console_store.py` already persists per-conversation events.
- `web/server.js` already exposes `GET /api/console/:id` and clear.
- `web/public/app.js` has `askTraceByConv` and `renderAskTraceForConv()`, but
  that Ask trace is browser-memory-only.
- `loadConsoleHistoryForConv()` currently reloads only `surface: "live"`,
  explicitly excluding Ask activity.

#### K.2.5.1 Add Ask-surface replay

**Status:** OPEN.

**Change:** Persist/replay the Ask trace through the same console store using a
clear surface discriminator, then hydrate `askTraceByConv` from the server.

**Priority:** P1.

#### K.2.5.2 Preserve crash/Stop pointer semantics

**Status:** OPEN.

**Change:** Close the small recovery hole where stale pending conversation
reclamation can preserve the console file but fail to attach the `consoleRef`
extra to the recovered conversation record.

**Priority:** P2.

#### K.2.5.3 Complete E.5 filter redesign

**Status:** OPEN.

**Current baseline:** three groups (`Output`, `Tools`, `Errors`) plus text search.

**Change:** Add the planned per-kind toggles/counts, regex option, tool-name
filter, turn scope, useful presets, and persisted filter preferences.

**Priority:** P2.

#### K.2.5.4 Clean up legacy console extras

**Status:** OPEN / maintenance.

**Change:** Remove or confine the old heuristic `console` extra after the store
is authoritative and all replay paths are migrated.

**Priority:** P3.

### K.2.6 Part J — final acceptance closure

**Status:** OPEN. The new notification-priority, clipboard-flood and 100k-token
regression rows added in revision 2026-09-22b are part of this closure pass.

**What it does:** Turns the plan from “implemented in source” into “verified as a
product.”

#### K.2.6.1 Tighten the 16 plan-added tool checklist entries

**Status:** OPEN.

**Priority:** P1.

**Acceptance:** a human has run each Ask/Debug recipe and corrected its `expect`
text, prerequisites and side-effect warnings.

#### K.2.6.2 Restore and execute all complex scenarios

**Status:** OPEN.

**Priority:** P1.

**Includes:** browser persistent login across separate `jarvis ask` processes,
forced ending, F.17 replays, key-health cooldown across restart, routing
regressions, clipboard watch, path refusals, and any real-provider scenarios
needed to validate the current behavior.

#### K.2.6.3 Close the release checklist only after P0/P1 items are green

**Status:** OPEN.

**Priority:** P1.

### K.2.7 Notification importance levels + clipboard flood protection

**Status:** OPEN — new requirement from the 2026-09-22 research pass.

**Current evidence:** `digest.py` already has `low` / `normal` / `high` plus
normalization aliases, but `notifier.notify()` persists only `kind`;
`clipboard_watch.py` currently emits one notification per matching clipboard
change; and the inbox cap limits retained records but does not limit live
delivery. The pieces therefore exist, but they are not yet one end-to-end
importance/flood policy.

#### K.2.7.1 Make priority first-class on notification records

**Status:** OPEN.

**Change:** Resolve a single `low` / `normal` / `high` priority from an
explicit caller value or per-kind default, persist it in every notification
record, and have `digest.py` consume that same resolved value.

**Priority:** P1.

**Acceptance:** `notify`, reminders, tasks and clipboard-watch notifications
all persist a valid priority; explicit overrides beat defaults; no second
priority vocabulary is introduced.

#### K.2.7.2 Add bounded clipboard burst coalescing

**Status:** OPEN.

**Change:** Add a small configurable debounce/coalescing policy to
`clipboard_watch`, keeping every event in the durable inbox while bounding the
number of pushed notifications. High priority bypasses suppression; normal and
low can coalesce; the coalesced notification reports the event count and latest
preview.

**Priority:** P1.

**Acceptance:** a rapid burst produces a bounded push count; an isolated change
remains prompt; the durable inbox still retains individual events; high-priority
watching cannot disappear silently.

#### K.2.7.3 Permanent regression coverage

**Status:** OPEN.

**Change:** Add focused tests for priority normalization, override-vs-default
behavior, digest interaction, clipboard burst coalescing, identical-repeat
handling, and the high-priority bypass.

**Priority:** P1.

### K.2.8 100k+ input-token burn containment from the supplied 2026-09-22 log

**Status:** OPEN — measured in the supplied JSONL trace.

**Baseline evidence:** 376 rows; 77 requests; 70 usage records; 123,368
reported input tokens; 2,027 output tokens; 572 appearances of the exact
unchanged user message `write_on_screen continue and enter` inside request
payloads. The later requests repeatedly carry prior history/tool material, and
several sequences repeat tool discovery plus an already-declined action. The
trace is strong evidence of input-context burn, but it does not by itself prove
that one single code path caused every repeated token.

#### K.2.8.1 Identify the redundant-context boundaries

**Status:** OPEN.

**Change:** Trace how the request builder carries repeated user messages, tool
results, tool schemas and confirmation/cancellation state across rounds and
across retries. Prefer measured duplication counts from the supplied fixture
over general assumptions.

**Priority:** P1.

#### K.2.8.2 Design a compact replay representation

**Status:** BLOCKED on K.2.8.1.

**Change:** Keep the minimum state required to continue the turn — current user
intent, relevant tool schemas/results, pending/declined action state and the
latest model text — without recopied unchanged transcript material. Preserve
all semantics needed by the model and the durable conversation record.

**Priority:** P1.

#### K.2.8.3 Make repeated cancellation/retry loops terminal

**Status:** BLOCKED on K.2.8.1.

**Change:** Once an explicit user decline has been recorded for the same action,
do not keep re-asking the model to perform that unchanged action in the same
logical turn. A genuinely new user request must still be able to re-authorize a
new action normally.

**Priority:** P1.

#### K.2.8.4 Replay the supplied log and add a permanent regression fixture

**Status:** OPEN.

**Change:** Redact secrets if needed, preserve the request/response shape, and
make the replay assert the before/after token metrics plus the important action
semantics. The test should fail if unchanged context starts growing without a
new source of information again.

**Priority:** P1.

**Acceptance:** the replay has a materially lower input-token total than the
123,368-token baseline; the same unchanged user/tool state no longer produces
monotonic context growth; explicit cancellation ends the repeated action loop;
and the final successful/declined action semantics remain correct.

## K.3 P2 — major remaining product functionality

### K.3.1 Part A §8 — real-time token streaming of text and thinking

**Status:** NOT STARTED.

**What it does:** Lets the user see model text and reasoning arrive while a turn
is running instead of waiting for the provider round to finish. This is the
missing complement to §5's interim-message narration.

#### K.3.1.1 Define a provider-neutral streaming callback/event shape

**Status:** OPEN.

**Change:** Add a shared event contract for text chunks, thinking chunks, turn
boundaries and errors without duplicating provider-specific UI logic.

**Priority:** P2.

#### K.3.1.2 Implement native streaming in each adapter

**Status:** OPEN.

**Change:** OpenAI-compatible, Anthropic, Gemini, Cohere and Ollama each need
provider-native stream parsing mapped into the shared event shape.

**Priority:** P2.

#### K.3.1.3 Thread stream events through `ai_client.ask()`

**Status:** OPEN.

**Priority:** P2.

#### K.3.1.4 Send stream events through the web transport

**Status:** OPEN.

**Change:** Add the minimal WS/SSE framing in `web/server.js` and preserve the
current console/Ask lifecycle semantics.

**Priority:** P2.

#### K.3.1.5 Render through one browser entry point

**Status:** OPEN.

**Dependency:** K.2.2 code-aware render pipeline should land first so streaming
and final rendering do not fork into two incompatible paths.

**Priority:** P2.

### K.3.2 D.1 — subagent live side view

**Status:** PARTIAL.

**What it does:** Lets the user see what each active subagent is doing near-live,
including saved tool calls, console lines and thinking, and cycle among active
subagents.

#### K.3.2.1 Near-live polling from conversation data

**Status:** OPEN.

**Change:** While a subagent is selected, poll `GET /api/conversations/:id`
incrementally so only new entries are rendered.

**Priority:** P2.

#### K.3.2.2 Add live daemon/console tail when applicable

**Status:** OPEN.

**Priority:** P2.

#### K.3.2.3 Dock the subagent view beside the main Ask panel

**Status:** OPEN.

**Dependency:** K.3.2.1.

**Priority:** P2.

#### K.3.2.4 Layer token streaming into the view

**Status:** BLOCKED until K.3.1 is available.

**Priority:** P2.

### K.3.3 G.2 — multiple checklist-group entries per module

**Status:** NOT STARTED.

**What it does:** Extends G.1 so a module can supply several checklist groups as
well as many tool entries.

#### K.3.3.1 Extend `TEST_CHECKLIST_GROUP`

**Status:** OPEN.

**Priority:** P2.

#### K.3.3.2 Update schema/loader/API/custom-tools editor

**Status:** OPEN.

**Priority:** P2.

#### K.3.3.3 Update Test Checklist merge + coverage tests + templates/docs

**Status:** OPEN.

**Acceptance:** built-in and user custom modules can provide more than one
named group with no duplicate or missing-group ambiguity.

**Priority:** P2.

### K.3.4 Part A §3 — expose MCP as a distinct Debug source

**Status:** PARTIAL.

**What it does:** Makes the Debug tool-source filter truthful for all tool
origins, including MCP.

**Current gap:** MCP tools are not merged into the ordinary `TOOL_SCHEMAS` /
`TOOLS` catalog, so they do not appear in the same source-labelled tool list.

**Priority:** P2.

### K.3.5 F.6 — capture the model's “last words” as structured result data

**Status:** PARTIAL.

**What it does:** Preserves the model's actual final textual intent separately
from the generated tool-call/result log so callers can distinguish “what the
model said last” from “what the harness inferred.”

**Current gap:** The shared `AIResult` path does not expose a dedicated field;
the current compact result shaping only surfaces `last_error`/logs.

**Priority:** P2.

### K.3.6 F.9 — true per-round pacing across adapter loops

**Status:** OPTIONAL / OPEN.

**What it does:** Prevents a fast inner agent from burning provider capacity in
rapid consecutive requests even when keys are isolated.

**Current state:** the earlier requirement's “or use a different key” half is
already satisfied by `spread_keys()`. This item is therefore an enhancement,
not a blocker for the delivered key-health behavior.

**Priority:** P3/P2 depending on measured provider rate-limit behavior.

## K.4 P2 — Part H UI rework

### K.4.1 Daemons panel

**Status:** BASELINE EXISTS / REWORK OPEN.

**Current:** status dot, running/crashed summary, start/stop/restart/status,
pid/adoption/next-start/last-error metadata, backend restart counters and
supervisor restart limits.

**Missing:** Test-Checklist-quality componentization, clearer crash-loop state,
first-class restart-count presentation, richer console interaction, keyboard
navigation and a dedicated panel module split from the giant `app.js`/`index.html`
monolith where appropriate.

**Priority:** P2.

### K.4.2 Backlog panel

**Status:** BASELINE EXISTS / REWORK OPEN.

**Current:** kanban-style columns, counts, project text, blocked-on text,
stale summary, card actions; no evidence that the final requested redesign has
been completed.

**Clarification:** The repo's `dragstart`/`dragover` implementation found during
audit belongs to the **saved-command step editor**, not the backlog kanban. Do
not use that code as evidence that backlog drag-reordering is implemented.

**Missing:** the planned high-quality backlog interactions, including the
remaining reordering/interaction behavior described by H.1, stronger live
search/filtering, clearer blocked roll-ups and polished empty/loading/error
states.

**Priority:** P2.

### K.4.3 Log search panel

**Status:** BASELINE EXISTS / REWORK OPEN.

**Current:** local conversation filtering plus explicit deep-search execution.

**Missing:** live-as-you-type deep search, query highlighting in results, richer
result navigation and the planned direct relationship to daemon console output.

**Priority:** P2.

### K.4.4 Schedules panel

**Status:** BASELINE EXISTS / REWORK OPEN.

**Current:** scheduled rows, kinds, next timing, errors, approval/resume/pause/
snooze/cancel actions.

**Missing:** the requested cleaner recurrence/next-run presentation, stronger
status visualization and the final panel-level grouping/filtering pass.

**Priority:** P2.

### K.4.5 Creation confirmation notifications

**Status:** OPEN.

**What it does:** Every successful scheduled-job creation should produce a clear
owner-facing confirmation notification, without treating direct one-shot
`notify` as a scheduled job.

**Current source:** `scheduler_tools.py` creation paths return job results but do
not emit the requested creation confirmation event.

**Priority:** P2.

## K.5 P2/P3 — validation and documentation debt

### K.5.1 Real browser validation

**Status:** OPEN.

**Needed for:** Part C browser tools/daemon, Test Checklist panel, Part I final
rendering and Part J complex scenarios.

**Priority:** P2.

### K.5.2 Real Windows validation

**Status:** OPEN.

**Needed for:** `run_shell` Windows argument behavior, cmd builtins, path move/
Recycle Bin semantics, clipboard PowerShell backend and the long-running
Tesseract PATH scenario.

**Priority:** P2.

### K.5.3 Real-provider validation

**Status:** OPEN.

**Needed for:** interim text on all five adapters, thinking-round behavior,
forced-ending edge cases, key-health pacing/cooldown and any future streaming.

**Priority:** P2.

### K.5.4 Reconcile `REPO_MAP.md`

**Status:** OPEN documentation housekeeping.

**Current gap:** The current map still needs explicit entries for the newly
landed `clipboard_tools.py`, `clipboard_watch.py`, `clipboard_cli.py` and
`browser_tools.py` modules (the earlier merge note already called this out).

**Priority:** P3.

### K.5.5 Keep test-checklist metadata truthful

**Status:** PARTIAL.

**Current:** exact tool-name coverage is complete. The remaining work is human
review of the “expect” language and real-world prerequisites/side-effect notes,
not creating more entries.

**Priority:** P2/P3.

### K.5.6 Strengthen tests for existing enhancement-only paths

**Status:** OPEN / test debt.

**Current evidence:** `tests/test_enhancements.py` documents that several
Enhancements #8–#10 were verified by one-off snippets rather than permanent
regression tests.

**Priority:** P3 unless a regression appears.

## K.6 Owner decisions / questions that remain meaningful

### K.6.1 Project-content discovery budget (F.2 follow-on)

**Status:** OPEN. See K.2.4.

### K.6.2 Part I highlighter source (D-I1)

**Status:** OPEN. Choose pinned CDN vs vendored asset after measuring bundle/
offline requirements.

### K.6.3 Fenced code in user bubble (D-I2)

**Status:** OPEN decision, with “render fences” as the documented candidate in
the historical plan.

### K.6.4 Shell-prompt stripping on code copy (D-I3)

**Status:** OPEN decision. The historical plan proposes stripping a leading
`$ ` only in clearly shell-like copied blocks; implement only after the owner
chooses the policy.

### K.6.5 Meaning of “every command” in `/` palette (D-I4)

**Status:** OPEN decision. The historical plan correctly calls out that blindly
passthrough-running every CLI subcommand would surface dangerous/irreversible
surfaces; settle the curated-vs-generic model before implementing the final
palette verbs.

### K.6.6 Extra `/` verbs (D-I5)

**Status:** OPEN decision. Existing candidate ideas are in Part I; do not add
verbs merely to fill a list.

## K.7 Recommended execution order

This is an execution dependency order, **not an evaluation/ranking of product
choices**. It is designed to remove blockers first and avoid rewriting the same
UI/rendering plumbing twice.

1. **P0. Fix subagent fail-closed parsing.** It is small, isolated and protects
   the key-isolation boundary.
2. **P0. Regenerate build metadata and rebuild/verify the executable.** Do this
   against the exact current source tree.
3. **P1. Restore F.17 fixtures and get the real-log replay harness executing
   again.** This gives the remaining loop work a stable regression anchor.
4. **P1. Fix I-B1 and I-B2.** They are independently reproducible and precede
   the Part I rendering/palette implementation.
5. **P1. Decide and implement the F.2 project-content discovery boundary.**
   Re-run Case 2b after the policy is explicit.
6. **P1. Implement K.2.8 100k+ token-burn containment** and replay the supplied
   JSONL baseline before declaring token-optimization work closed.
7. **P1. Implement K.2.7 notification importance levels + clipboard flood
   protection**, then run the Part J burst/digest acceptance cases.
8. **P1. Finish E Ask-console replay.** This closes a real persistence gap and
   gives later UI rework a stable source of truth.
9. **P1. Execute Part J's human tightening + complex scenario pass** for all
   currently delivered tools/features, including the three new regression areas.
10. **P2. Build Part I's render pipeline/code blocks and `/` palette** after the
    I-B1/I-B2 foundations are fixed and the rendering entry point is stable.
11. **P2. Build Part A §8 streaming** and feed both final Ask rendering and the
    D.1 subagent live view through the shared stream/event path.
12. **P2. Finish D.1, G.2 and Part H rework.** These are now UI/product work
    against relatively stable backend contracts.
13. **P2/P3. Close OS/provider verification and documentation debt**, then
    tighten permanent regression coverage where tests still depend on one-off
    snippets.

## K.8 “Done” definition for this master plan

The plan should not be considered fully closed merely because source files for
the listed features exist. The master plan is **complete** when all of the
following are true:

- [ ] P0 subagent malformed-pool cases fail closed and the test passes.
- [ ] Build metadata hash matches the source tree and the executable is rebuilt
      and verified from that exact tree.
- [ ] F.17 fixtures exist and both real-log replay cases execute.
- [ ] I-B1 and I-B2 regressions are fixed with permanent tests.
- [ ] F.2 project-content discovery budget policy is decided and implemented,
      or explicitly rejected/closed by the owner with the historical case
      reclassified accordingly.
- [ ] Part E Ask-console output survives reload by replaying from the persistent
      console store, with live behavior still intact.
- [ ] §8 streaming is implemented or explicitly removed from the product goal.
- [ ] D.1 subagent live view is actually near-live and tested on a real web UI.
- [ ] G.2 supports multi-group module-supplied checklist data.
- [ ] Part H reaches the requested Test-Checklist-quality UI bar.
- [ ] Notification records carry the shared `low` / `normal` / `high` importance
      level, and the digest/clipboard paths honor it consistently.
- [ ] Clipboard-watch burst tests prove notifications are bounded/coalesced
      without losing individual durable inbox events.
- [ ] The supplied 100k+ token-burn replay shows bounded repeated-turn context
      growth and a materially lower input-token total than the 123,368-token
      baseline, with action/cancellation semantics preserved.
- [ ] Part I code-block and slash-palette acceptance tests exist and pass.
- [ ] Part J is manually executed across a real model/UI for the scenarios it
      claims to cover.
- [ ] `REPO_MAP.md` and the current docs no longer contain status statements
      that contradict the authoritative Part 0 / Part K snapshot.

## K.9 Audit limitations

This audit is a source-and-test review of the provided archive, not a live
production certification. In particular, the sandbox does not provide the
user's Windows desktop, a real Chromium session with the user's login state,
all external model/API credentials, or every OS-native clipboard/backend. Those
limitations are called out as verification gaps rather than converted into
source-code failures.

