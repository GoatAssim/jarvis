# Jarvis Session Handoff — 2026-09-14

Continuation doc for a fresh chat. Assume zero prior context beyond what's written here.
This chat is about to expire, so this is meant to be self-contained.

## 1. What "jarvis" is

The user's personal local AI assistant project, `jarvis-cli` (Python backend) +
`web/server.js` (Node/browser frontend, the "web UI"). **Every enhancement or fix
discussed in this project must land in BOTH surfaces** — CLI and web frontend —
not just one. This was an explicit, repeated user requirement.

Backend architecture: stateless REST tool-calling loop against Gemini (and other
OpenAI-compatible/Anthropic/Cohere/Ollama providers via `ai_providers.py` adapters),
with multi-key failover per provider. Currently running `gemini-3.6-flash`, user is
open to dropping to `gemini-2.5-flash` if needed. Key files: `ai_client.py` (message/
prompt building, `ask()` orchestration), `ai_providers.py` (5 provider adapters, each
with its own tool-calling round loop), `conversations.py` (per-conversation history,
stored at `~/.jarvis/conversations/<id>.json`), `tools.py` (master tool schema/dispatch
merge), `tool_registry.py` (router groups/keywords/pack instructions), `tool_result_
shaping.py` (table-driven per-tool result trimming by capacity-mode verbosity),
`git_tools.py`, `memory.py` (long-term fact store + prompt injection), `tool_safety.py`
(confirm-required tool list).

## 2. What's already been done this session — 4 bugs found & fixed, patches delivered

All four fixes were verified (syntax-checked, dry-run `patch -p1` applied cleanly
against a fresh extraction of the user's repo zip) and delivered as separate patch
files the user already has locally: `jarvis-token-fixes.patch`, `jarvis-token-
fixes-2.patch`, `jarvis-token-fixes-3.patch`. If continuing this work in a new
session, ask the user to re-attach these if the actual diffs are needed — summaries
below should be enough to review without them.

### Fix 1 — `git_commit_all` tool (patch 1: ai_client.py, ai_providers.py,
git_tools.py, tool_registry.py)

Root cause: a git log showed a simple "commit everything" request took **13 rounds**
of tool calls (status → diff → add → commit, sometimes retried), each round resending
the entire growing conversation to a stateless API — classic quadratic token blowup.

Fix: added `tool_git_commit_all(args)` to `git_tools.py` — does `git add -A` then
`git commit -m <message>` in one Python subprocess call instead of two separate
`git_run` tool round-trips. Output capped at 800 chars (it's a confirmation, not a
diff to review). Registered in `GIT_TOOL_SCHEMAS`/`GIT_TOOLS` (auto-picked-up
everywhere `git_run` was, since `tools.py` spreads those lists wholesale). Added to
`tool_registry.py`'s `system_control` group with router keywords (`commit`, `stage
and commit`, `commit everything`) and updated `TOOL_PACK_INSTRUCTIONS`. Updated the
GIT line in `ai_client.py`'s `_tools_blurb()` to tell the model to prefer it and skip
status/diff unless the user wants changes reviewed first. Did NOT add it to
`tool_safety.py`'s confirm-required list — it's additive-only (no force/reset/push),
consistent with how destructive ops are gated elsewhere.

### Fix 2 — cross-key `RoundBudget` (patch 1 also, same files as above, ai_providers.py
+ ai_client.py primarily)

Root cause (raised by the user, quoting a ChatGPT analysis that was correct on this
point): `MAX_TOOL_ROUNDS = 5` in `ai_providers.py` is enforced **per provider-key
attempt**. When a key hit a 429 mid-loop and failed over to the next key, the round
counter reset to 0 — so a key that burned all 5 rounds and failed handed the (already
much bigger) transcript to the next key, which got its OWN fresh 5 rounds. Loop
counter reset; the growing history it was resending every round did not.

Fix: added `GLOBAL_MAX_TOOL_ROUNDS = MAX_TOOL_ROUNDS + 1` (6) and a `RoundBudget`
class (`.take()` consumes one round, returns False once exhausted; `.remaining()`)
to `ai_providers.py`. All 5 adapters (`call_openai_compatible`, `call_anthropic`,
`call_gemini`, `call_cohere`, `call_ollama`) now accept an optional `round_budget=None`
param (default: fresh `RoundBudget()` for backward compat). Every "should I advertise
tools this round" gate now also checks `round_budget.remaining() > 0`; every "should I
actually execute a tool call" gate now also requires `round_budget.take()` to succeed.
In `ai_client.py`'s `ask()`, one `RoundBudget()` is instantiated before the
provider/key failover loop and passed into every `adapter(...)` call — same object
reused across every key/provider tried for that one user request. Net effect: worst
case for one `ask()` is now 6 tool rounds total across ALL keys/providers combined,
not 5 × number_of_keys_tried.

### Fix 3 — `search_files` result trimming (patch 2: tool_result_shaping.py only)

Root cause: a log showed round-0→round-1 input tokens jumping 1,188 → 4,302 on a
routine file search. Traced to `search_files` (Everything SDK wrapper,
`everything_tools.py`) returning all 30 default results, each carrying `path`,
`name`, `is_folder`, `size_bytes`, `date_modified` — and `tool_result_shaping.py`'s
`TOOL_RESULT_SPECS["search_files"]` only dropped `size_bytes`/`date_modified` at
`low` verbosity, NOT at `medium` (which is what 100% Capacity mode — what the user
was actually running — uses).

Note: a ChatGPT-authored analysis of this same log also claimed the fix belonged in
`conversations.py` (stripping `extras` payloads) and that no global truncation
existed. Both were wrong — the leak is within a single live `ask()`'s tool-call loop,
not `conversations.py`'s cross-turn replay (a completely different code path), and a
global 4000-char cap (`MAX_TOOL_RESULT_CHARS` in `ai_providers.py`'s
`_stringify_tool_result()`) already existed. Called this out explicitly to the user
rather than implementing the wrong fix.

Fix: extended `search_files`'s `list_item_drop` spec to also drop `size_bytes`/
`date_modified` at `medium` verbosity (previously `low` only) — neither field is used
by `present_file`/`open_file` at any verbosity. Verified via a synthetic 30-result
payload matching the log's shape: **27% size reduction**.

### Fix 4 — recent-history window ordering bug (patch 3: conversations.py only)

Root cause (raised by the same ChatGPT analysis, and this time it was RIGHT — verified
by reproducing it): `conversation_messages()`'s recent-window loop
(`CONTEXT_EXCHANGES=10`, `CONTEXT_CHAR_BUDGET=4800`) took the last 10 exchanges in
chronological order (oldest-of-the-10 first) and iterated **forward**, accumulating
a running `used` char count and `break`-ing once the budget was exceeded. This means
if the OLDER exchanges within that 10-window were verbose, the budget could be
exhausted before the loop ever reached the actual most recent exchange — silently
dropping "what did you just say"-type recent turns while keeping older, less relevant
ones.

Reproduced directly: simulated 9 verbose exchanges (500+700 chars each, near the
`MAX_USER_CHARS`/`MAX_ASSISTANT_CHARS` caps) followed by one short final exchange
("what did I just say" / "you said X"). Before fix: only 4 of 10 pairs kept, and the
final exchange was NOT among them. After fix: same 4-of-10 kept, but now it's the 4
MOST RECENT, with "what did I just say" correctly present and last in the list.

Fix: loop now iterates `reversed(recent_src)` (newest-first), collecting into
`kept_pairs` (same "squeeze the first-considered item if it doesn't fully fit, else
break" logic, just applied to the newest-first item now), then flattens via
`reversed(kept_pairs)` before extending `messages` — so the budget is spent on recent
turns first, and final ordering sent to the model is still correct chronological order.

## 3. Mark LIII research — a different jarvis-like project found online

User uploaded `Mark-LIII-main.zip` (github.com/FatihMakes/Mark-LIII) and asked for
research + a port plan. Key finding stated up front and repeatedly: **it's built on
the Gemini LIVE API** (one persistent bidirectional voice session), not a stateless
REST loop like jarvis. This means its "token handling" doesn't map 1:1 — it uses
`context_window_compression=types.ContextWindowCompressionConfig(sliding_window=
types.SlidingWindow())` in `main.py`'s `LiveConnectConfig`, i.e. Google's own infra
manages the context window server-side for the life of the session. System prompt +
tool declarations are built ONCE per connect, not resent per round (there are no
discrete rounds in a live stream). None of the 4 bugs above have an equivalent in
Mark LIII — not because it solved that problem, but because its architecture
sidesteps it entirely. This can't be "ported" into jarvis without switching jarvis to
the Live API, which is a much bigger call than a patch — explicitly flagged as such
to the user.

### What WAS identified as genuinely portable (in priority order used in the plan doc):

**§3.1 — Memory core/index/recall cap.** Mark LIII's `memory/memory_manager.py`,
`format_memory_for_prompt()`: identity fields always ride in full; everything else is
most-recently-updated-first, capped at `PROMPT_CORE_CHARS=900` with a per-category cap
(`PROMPT_MAX_PER_CATEGORY=6`) so one chatty category can't crowd out the rest; anything
that didn't fit gets listed by KEY ONLY in a trailing index (capped at
`PROMPT_INDEX_CHARS=420`) so the model knows a fact exists and can look it up via
`recall_memory` instead of either never seeing it or blowing the budget. Measured: 971
chars for 62 stored facts. **This is the item currently under review — see §5 below.**

**§3.2 — Fully self-describing, auto-discovered tools.** Mark LIII's
`core/action_loader.py` scans `actions/*.py` for a module-level `TOOL` dict (`name`/
`description`/`parameters`/`handler`), auto-discovers at startup, dispatches via
signature introspection. Jarvis already does a PARTIAL version of this (`git_tools.py`'s
`GIT_TOOL_SCHEMAS`/`GIT_TOOLS` get imported and spread into `tools.py`'s master lists —
this is how `git_commit_all` slotted in cleanly in Fix 1), but it's still manual
imports + manual list-spreading in `tools.py`. Plan: write a `tool_loader.py` that does
real directory scanning, replacing the manual import list.

**Explicit later addition to §3.2 (user-requested):** this new tool system MUST ship
with a template file (mirroring Mark LIII's own `plugins/_template.py`) containing
EVERY instruction needed for an AI to write a complete, correct tool-plugin file from
the template alone — schema shape, the `_NAME_RE`-equivalent naming constraint, how
confirm-gating should work per §3.3 (NOT a model-fillable param), where handler
signature-introspected context args come from, how to register with
`tool_result_shaping.py` if output needs trimming. Explicitly justified as: this is
exactly what I (Claude) had to reverse-engineer across `tool_loader.py`/
`tool_registry.py`/`tool_result_shaping.py` to wire in `git_commit_all` correctly this
session — the template exists so nobody (AI or human) has to repeat that.

**§3.3 — A confirmation token the model can't forge.** Mark LIII's README describes
their own old bug bluntly: `confirmed` used to be a tool parameter the MODEL filled
in — nothing verified a human ever saw it. Fixed via a UI-issued token / banner +
human button press, action runs only on that press, nothing blocks while waiting.
Directly relevant: jarvis's `git_tools.py`'s `_need_confirm()` reads `confirm` straight
out of model-supplied `args` — same shape as their old bug. Low-stakes today (worst
case: model-issued `git reset`/`clean`), but flagged as the thing to fix BEFORE adding
any action more destructive than git.

**§3.4 — Undo (optional/lower priority).** `core/undo.py` — appends a closure per
reversible action to an in-memory list, near-zero runtime cost until invoked. Only
worth it if jarvis grows unattended file/setting writes worth being able to reverse.

**§3.5 — TTS/STT, local + online providers (explicitly user-requested addition).**
From `core/tts.py`/`core/stt.py`:
- TTS: Kokoro (local/offline, ~330MB neural model), Edge TTS (online/free, MS TTS, no
  API key), ElevenLabs (online/paid, best quality, needs API key)
- STT: faster-whisper (local/offline, VAD-buffered, ~75-290MB one-time download then
  fully offline), Vosk (local/offline, lighter, streaming)
Noted: since jarvis is REST-based not a persistent Live session, this would sit
OUTSIDE the tool-calling loop entirely (record → transcribe → normal `ask()` → speak),
not wired into the request cycle the way Mark LIII does it.

**§3.6 — Agent-based code editor (explicitly user-requested addition).**
`actions/dev_agent.py` — plan (`_plan_project`, 1 model call → file list + deps + run
command) → write files → install deps → run with timeout → self-fix loop
(`_parse_traceback` + `_classify_error` + retry, capped at `MAX_FIX_ATTEMPTS=5`) →
optionally open VS Code. Uses two separate configurable models (`MODEL_PLANNER`/
`MODEL_WRITER`, both `gemini-flash-latest` in their repo) so planning/writing could
use different tiers. Flagged explicitly: this is architecturally the closest thing in
Mark LIII to a Claude-Code-style agent, and its `MAX_FIX_ATTEMPTS=5` is a standalone
hardcoded constant, uncoordinated with anything else — exactly the failure PATTERN
that caused Fix 2 above (the cross-key round budget bug). Recommendation: if built,
wire its retry cap into jarvis's existing `RoundBudget` rather than growing a second,
separate, uncapped-relative-to-everything-else retry loop.

**Explicitly NOT worth porting:** wake word, instant acknowledgment, measured audio-
device picker — all Live-session/voice specific, not applicable unless jarvis becomes
voice-first end-to-end (§3.5 would be a prerequisite for that, but doesn't require
these).

## 4. Bug fixes tab (user-requested addition, NOT yet investigated/fixed — just
documented as known issues)

1. **Console speech bubble no longer renders.** The `"console"` extras type
   (`_split_console_dump` in `ai_client.py`, which does
   `extras.append({"type": "console", "data": {"dumpLines": dump_lines}})`) used to
   show tool-call/dump output as its own bubble in the transcript. It no longer
   appears. Needs a repro + trace from `_split_console_dump` through to whatever the
   web frontend does with an exchange's `extras` on render (the `renderThreadExtra`-
   equivalent) — likely broke somewhere in that chain. NOT YET DEBUGGED — I don't have
   the web frontend source, only the `jarvis-cli` backend tree.

2. **`present_file` doesn't persist across conversations (web frontend).**
   Conversation history itself persists correctly — verified this IS working, stored
   per-conversation as JSON under `~/.jarvis/conversations/<id>.json` via
   `conversations.py`. But a file shown via `present_file` in one turn isn't still
   shown/openable when reopening that same saved conversation later — behaves like
   transient UI state rather than being saved as part of the exchange's `extras`
   (same mechanism as bug #1 above). Hypothesis (not confirmed): both bugs may share
   a root cause since both are "extras don't survive/render on the web frontend."
   Flagged in the plan doc to fix together, before/alongside §3.1's memory panel (if
   any) and before §3.6's dev-agent output streaming, since both would also depend on
   `extras` working correctly.

## 5. CURRENT OPEN ISSUE — reviewing §3.1's implementation (memory.py diff)

**This is what a continuing session needs to pick up.**

Another AI (not me, not further specified by the user — described only as "another
ai") implemented §3.1 against jarvis's actual `memory.py`, and explicitly did NOT do a
literal port of Mark LIII's design — reasoning given (by that AI, relayed by the user):
jarvis already does query-relevance-scored retrieval, which is BETTER than Mark LIII's
pure-recency-only core, so only the two genuinely missing disciplines were added on
top of the existing relevance system: (a) identity-facts-always-included, (b) labeled
overflow index instead of a bare count. The user then uploaded the actual diff
(`memory_py.diff`) for review.

I had already raised two concerns BEFORE seeing this diff (from the implementing AI's
own summary alone):
1. Is the identity-always-included block itself budget-capped, or could it become a
   new unbounded-growth vector (i.e., the same shape of bug as fixes 1-4 above, just
   relocated)?
2. Is the new overflow INDEX itself char-budgeted, or could a large number of
   matched-but-excluded facts bloat the prompt via the index instead of the values?

**I then read the actual diff and completed this review** (this is the last thing
that happened before this handoff was requested). Full diff content and my analysis
below — this is the part a continuing session most needs.

### The diff (memory_py.diff, in full)

```diff
--- orig/memory_original.py
+++ jarvis/jarvis-cli/jarvis/memory.py
@@ -4,9 +4,12 @@
 notebook: names, preferences, hardware, 'remember that…' facts.

 Facts are NOT dumped into every prompt. Each ask retrieves only facts that
-look relevant to the user message (and recent user turns). Explicit 'what do
-you remember' style questions load as many as the budget allows. The model
-can still memory_search if retrieval misses.
+look relevant to the user message (and recent user turns), except identity
+facts (name, timezone, etc.), which ride along in full every time. Explicit
+'what do you remember' style questions load as many as the budget allows.
+Anything relevant that didn't fit the budget is listed by label in a
+trailing index so the model can still memory_search it by name instead of
+guessing it doesn't exist.
 """

 import json
@@ -25,9 +28,26 @@
 PROMPT_FULL_BUDGET = 1200
 PROMPT_COMPACT_BUDGET = 450
 MAX_PROMPT_FACTS = 8
+# Ported from Mark LIII's memory_manager.PROMPT_MAX_PER_CATEGORY: caps how many
+# facts sharing a primary tag may occupy the core block, so one chatty tag
+# (e.g. a dozen "games" facts) can't crowd out everything else that matched.
+PROMPT_MAX_PER_TAG = 3
+# Budget for the trailing "also remembered" index of facts that matched but
+# didn't fit. Same idea as Mark LIII's PROMPT_INDEX_CHARS: the model can't
+# decide to memory_search something it doesn't know exists, so instead of a
+# bare "(N other facts not shown)" count we hand back the actual labels.
+PROMPT_INDEX_CHARS = 300

 _KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
 _WORD_RE = re.compile(r"[a-z0-9]{2,}")
+# Facts tagged "identity" (or keyed as one of these) ride in every prompt,
+# in full, regardless of query relevance — same rationale as Mark LIII's
+# _IDENTITY_FIELDS: it's wrong for the assistant to have to guess or search
+# for the user's own name.
+_IDENTITY_KEYS = frozenset({
+    "name", "preferred_name", "user_name", "pronouns", "timezone",
+    "location", "city", "birthday", "job", "role",
+})
 _RECALL_ALL = re.compile(
     r"\b(what do you (remember|know)|who am i|about me|"
     r"(list|show|dump) (my |your )?(memory|memories|facts)|"
@@ -159,7 +179,51 @@
     return line


-def _render_facts(facts, budget, omitted=0):
+def _fact_label(f):
+    """Short, greppable label for a fact — what goes in the overflow index so
+    the model has something concrete to pass to memory_search."""
+    key = (f.get("key") or "").strip()
+    if key:
+        return key.replace("_", " ")
+    tags = f.get("tags") or []
+    if tags:
+        return str(tags[0])
+    text = (f.get("fact") or "").strip()
+    return (text[:24] + "…") if len(text) > 24 else (text or "fact")
+
+
+def _is_identity_fact(f):
+    return "identity" in (f.get("tags") or []) or (f.get("key") or "") in _IDENTITY_KEYS
+
+
+def _render_index(overflow_labels):
+    """Render the trailing 'also remembered' line from a list of labels,
+    deduped and capped to PROMPT_INDEX_CHARS so the index can't itself blow
+    the budget it exists to protect."""
+    seen = set()
+    names = []
+    idx_budget = PROMPT_INDEX_CHARS
+    extra = 0
+    for label in overflow_labels:
+        if label in seen:
+            continue
+        seen.add(label)
+        if idx_budget - len(label) - 2 < 0:
+            extra += 1
+            continue
+        names.append(label)
+        idx_budget -= len(label) + 2
+    if not names:
+        return ""
+    line = (
+        "(Also remembered, not shown here — call memory_search with one of "
+        "these to read it: " + ", ".join(names)
+        + (f", +{extra} more" if extra else "") + ")"
+    )
+    return line
+
+
+def _render_facts(facts, budget, overflow_labels=None):
     header = (
         "Relevant long-term memory (trust these over chat recap; "
         "memory_search if something is missing; memory_save / memory_forget to change):"
@@ -167,21 +231,34 @@
     lines = [header]
     used = len(header)
     included = 0
-    for f in facts:
+    overflow_labels = list(overflow_labels or [])
+    for j, f in enumerate(facts):
         line = _format_fact_line(f)
         if used + len(line) + 1 > budget:
-            omitted += len(facts) - included
+            overflow_labels.extend(_fact_label(x) for x in facts[j:])
             break
         lines.append(line)
         used += len(line) + 1
         included += 1
-    if omitted > 0:
-        lines.append(f"({omitted} other facts not shown — memory_search if needed.)")
-    return "\n".join(lines) if included else ""
+    if not included and not overflow_labels:
+        return ""
+    index_line = _render_index(overflow_labels)
+    if index_line:
+        lines.append(index_line)
+    return "\n".join(lines) if included or index_line else ""


 def prompt_context(char_budget=None, compact=False, query="", extra_texts=None):
-    """Return memory lines relevant to query, or '' if nothing matches."""
+    """Return memory lines relevant to query, or '' if nothing matches.
+
+    Identity facts (tag "identity", or a well-known key like name/timezone)
+    always ride along in full. Everything else still has to match the query
+    to be considered, then competes for the remaining budget — capped per
+    primary tag (PROMPT_MAX_PER_TAG) so one chatty tag can't crowd out the
+    rest — and whatever matched but didn't fit is listed by label in a
+    trailing index instead of a bare count, so the model can still
+    memory_search it by name.
+    """
     facts = load_facts()
     if not facts:
         return ""
@@ -192,24 +269,44 @@
     blob = " ".join([query or ""] + [t for t in extras if t])
     if _RECALL_ALL.search(blob or ""):
         newest_first = list(reversed(facts))
-        return _render_facts(newest_first, budget, omitted=0)
+        return _render_facts(newest_first, budget)

+    identity_facts = [f for f in facts if _is_identity_fact(f)]
     qtoks = _expand(_tokens(blob))
+
     if not qtoks:
-        return ""
+        if not identity_facts:
+            return ""
+        return _render_facts(identity_facts, budget)

     ranked = []
     for i, f in enumerate(facts):
+        if _is_identity_fact(f):
+            continue
         s = _score_fact(f, qtoks)
         if s <= 0:
             continue
         ranked.append((s, i, f))
-    if not ranked:
-        return ""
     ranked.sort(key=lambda x: (-x[0], -x[1]))
-    chosen = [f for _, _, f in ranked[:MAX_PROMPT_FACTS]]
-    omitted = max(0, len(ranked) - len(chosen))
-    return _render_facts(chosen, budget, omitted=omitted)
+
+    if not identity_facts and not ranked:
+        return ""
+
+    chosen = list(identity_facts)
+    tag_used = {}
+    overflow_labels = []
+    for _s, _i, f in ranked:
+        if len(chosen) - len(identity_facts) >= MAX_PROMPT_FACTS:
+            overflow_labels.append(_fact_label(f))
+            continue
+        primary_tag = (f.get("tags") or [None])[0]
+        if tag_used.get(primary_tag, 0) >= PROMPT_MAX_PER_TAG:
+            overflow_labels.append(_fact_label(f))
+            continue
+        chosen.append(f)
+        tag_used[primary_tag] = tag_used.get(primary_tag, 0) + 1
+
+    return _render_facts(chosen, budget, overflow_labels=overflow_labels)


 def tool_memory_save(args):
```

### My review verdict (COMPLETED — both prior concerns resolved, 2 minor
non-blocking observations)

**Concern 1 (identity block budget-capped?) — RESOLVED correctly.** Identity facts
are placed first into `chosen`, then flow through `_render_facts`'s single shared
`used`/`budget` accounting exactly like every other fact — there's no separate
uncapped-inclusion path. No new unbounded-growth vector was introduced. Minor
edge-case worth knowing (not a bug): if a user accumulates enough identity-tagged
facts to exceed `budget` on their own, the render loop's break logic would push the
overflow identity facts (and everything after them) into the labeled overflow index —
meaning identity facts COULD, in a pathological case, get silently demoted from
"always full" to "in the index" once there are enough of them. Worth keeping an eye
on, not worth blocking on.

**Concern 2 (overflow index itself char-budgeted?) — RESOLVED correctly.**
`_render_index()` explicitly enforces `PROMPT_INDEX_CHARS = 300`, dedupes via a `seen`
set, and collapses anything beyond that budget into a `+N more` suffix instead of
listing it — this directly mirrors Mark LIII's own `PROMPT_INDEX_CHARS` pattern.
Worth flagging as a documentation note (not a bug): the index line is appended AFTER
the main budget-checked loop and isn't itself counted against the `budget` param
passed in — so the TRUE worst-case total injected size is `budget + PROMPT_INDEX_
CHARS` (≈1500 chars in full mode, ≈750 in compact), not `budget` alone. This matches
both the ORIGINAL code's behavior (the old bare-count suffix was also appended
post-budget with no further enforcement) and Mark LIII's own design (their
`PROMPT_CORE_CHARS` + `PROMPT_INDEX_CHARS` are also two separate, summed budgets) —
so this is a small, fixed, predictable overhead, not unbounded growth. Just worth
documenting as the actual ceiling if anyone tunes these constants later.

**Two other minor observations, both fine:**
- `MAX_PROMPT_FACTS` correctly counts only NON-identity chosen facts
  (`len(chosen) - len(identity_facts) >= MAX_PROMPT_FACTS`), so identity facts don't
  eat into the query-relevant fact quota. Good.
- `PROMPT_MAX_PER_TAG` buckets by `(f.get("tags") or [None])[0]` — facts with NO tags
  at all share a single `None` bucket, capped at 3 total regardless of how many
  untagged-but-relevant facts exist. Minor edge case if a lot of facts end up
  untagged; not a bug, just a behavior to be aware of.
- Bonus: the `_RECALL_ALL` ("what do you remember") branch now also benefits from the
  labeled-overflow-index behavior for free (previously just a bare count), since it
  flows through the same generalized `_render_facts`.

**Overall: implementation is correct and complete for what §3.1 asked for.** The
implementing AI's `python3 -m py_compile` pass and the described smoke test (temp
`~/.jarvis`, seeded facts, multiple query shapes) match what the diff's logic should
produce. No other file touches `_render_facts`'s signature, so this is self-contained
to `memory.py` as claimed.

**Recommendation for continuation: mark §3.1 DONE in the plan doc (`jarvis-
enhancement-plan.md`, which the user has locally in their outputs — not reproduced
in full here, but its structure/content is summarized in §3 above).** Optional
nice-to-have, not required: a test asserting worst-case output length is bounded by
`budget + PROMPT_INDEX_CHARS`, given this codebase's pattern of prior bugs all being
"a real trimming mechanism with an uncapped edge somewhere adjacent to it" — this
diff doesn't have that problem, but a regression test would catch it if a future
edit introduces one.

## 6. Suggested next steps for a continuing session

1. Mark §3.1 done (per the review above).
2. Move to §3.2 — tool auto-discovery + the required template file.
3. §3.3 — confirmation-token audit (`git_tools.py`'s `_need_confirm()`), before
   adding anything more destructive than git.
4. §4 bug fixes — console speech bubble + `present_file` persistence (likely same
   root cause; needs web frontend source, which hasn't been provided/read yet).
5. §3.5 TTS/STT, §3.6 dev agent (wire retry cap into `RoundBudget`), §3.4 undo
   (optional) — in that order, per the plan doc's final priority list.

## 7. Files that exist locally (user has these, not reproduced/attached here)

- `jarvis-token-fixes.patch`, `jarvis-token-fixes-2.patch`, `jarvis-token-fixes-3.patch`
  — the 4 bug fixes from §2 above, ready to `patch -p1` in order.
- `jarvis-enhancement-plan.md` — the full Mark LIII comparison + port plan (§3 above
  is a summary of its content; sections numbered 1–6 plus 3a in the actual file).
- `memory_py.diff` — the §3.1 diff reviewed in full in §5 above.
- Original uploads for provenance, not needed for continuation: two `.jsonl` request
  logs (`18d531a4ed56aef8.jsonl`, `fd83ab9dd1708011.jsonl`) that fixes 1–4 were
  diagnosed from, and `Mark-LIII-main.zip` (github.com/FatihMakes/Mark-LIII) that
  §3 was researched from.