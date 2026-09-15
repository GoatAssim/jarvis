# Token optimization + skills — what was implemented

Implements `skills-and-token-optimization-research.md` against this repo.
Written for whoever reads the diff next; the reasoning that isn't obvious
from the code is here or in the relevant module docstring.

## Starting point

The repo already had most of lever 2. `tool_router.route()` narrows the
catalog by keyword with no model round trip, `search_tools` + `_discover_sink`
is a hand-built equivalent of Anthropic's Tool Search, and
`compact_schemas_for_prompt` / `name_only_schemas_for_prompt` /
`schemas_for_tools` already gave three schema tiers. Measured before starting:

| Tier | Est. tokens |
|---|---|
| Full session catalog (67 schemas) | 10,614 |
| Compact | 4,798 |
| Name-only | 2,433 |
| System prompt (compact mode) | 260 |

Per-group cost showed where the remaining waste was: `playnite` 31 tools /
2,699 tok, `desktop` 16, `system_control` 11. Everything else is under 10.

## 1. Prompt caching (lever 3) — was entirely absent

### The load-bearing change

`_system_prompt()` is now a thin join over `_system_prompt_parts()`, which
returns `(static_prefix, per_request_tail)`.

Every provider here caches a byte-**prefix**. The old prompt interleaved
static text (persona, tools blurb, pack instructions) with per-request text
— most importantly `memory.prompt_context(query=user_text)`, which differs on
literally every turn. That put query-dependent bytes *in front of* static
ones, so the prefix changed every turn and no cache could ever hit. No
quantity of `cache_control` markers fixes that; the bytes genuinely differ.

The split is what makes all four providers below work. Verified byte-identical
when rejoined (`test_prompt_cache.py`), so nothing the model sees changed.

Part ordering is unchanged. `extra_instructions` is static and would cache
slightly better hoisted into the prefix, but moving it changes the prompt the
model sees, and a token optimization isn't worth an unmeasured behavior
change.

### Per provider

**Anthropic** — `cache_control: {"type": "ephemeral"}` on the static system
block. The non-obvious part: the default model `claude-haiku-4-5` has a
**4,096-token floor**, and the system prompt is 160–830 tokens. Marking system
alone would never once hit. It only clears the floor because prefixes are
cumulative (`tools → system → messages`), pulling the tool schemas in with it.
So the code marks system and *size-checks tools+system*. Measured on a real
prompt: 5,547 tokens, clears the floor.

A real consequence, pinned as a test: **ultra mode (name-only, ~2,400 tok)
cannot use caching on the default model.** The correct response is no
breakpoint plus a logged reason, not a 1.25x write premium on a prefix that
can never be read.

Tools get a second breakpoint only via `prompt_cache_tools: true`. Off by
default because a write never read costs 1.25x, and the tool list is the part
of the prefix most likely to differ between two asks.

**Gemini** — this was the jank. The old code created a `cachedContents`
resource per ask and **deleted it in a `finally`**. So every fresh `jarvis`
process paid a creation round trip on the latency path, creation billing, and
storage — to collect a discount only on rounds 2+ of that one ask. Most asks
are a single round, so it was usually a net loss, and structurally could never
be otherwise, because the cache was destroyed before the next process could
reach it.

Now: implicit caching (free, automatic on 2.5+, needs only a stable prefix) is
the default. Explicit is opt-in, and when on, cache names persist across
processes via `gemini_cache_store.py`, keyed on a fingerprint of exactly what
went into the cache. The happy-path delete is gone; the only remaining delete
is for a cache that genuinely went stale mid-ask.

**Groq / OpenAI family** — caches automatically above ~1024 tokens with no
opt-in and no fee, so the work was the prefix ordering. The one request-side
lever is `prompt_cache_key`, a stable per-conversation string that routes to
the same backend node. A hint, not a guarantee. Suppressible per provider for
strict self-hosted endpoints.

**Ollama** — the lever is `keep_alive`, defaulted to 30m. Ollama reuses a KV
prefix automatically but dumps it when it unloads the model, which it does
after 5 minutes idle. With a fresh process per call and gaps between them, the
default guaranteed a cold prefill on most asks.

Also documented, because it will otherwise waste someone's afternoon:
`prompt_eval_count` is **not** a cache signal on Ollama. It reports the size of
the prompt sent, not tokens recomputed, so it is flat across a hit and a miss
alike. `prompt_eval_duration` is what moves; that's what `token_usage`
surfaces.

### Observability

Caching fails silently — a prefix that never matches returns a correct answer
at full price with no error anywhere. So `token_usage.extract_usage` now
reports `cache_read_tokens` / `cache_write_tokens` separately (Anthropic's
`cache_*_input_tokens`, Gemini's `cachedContentTokenCount`, OpenAI-family's
`prompt_tokens_details.cached_tokens`) instead of folding them away, and
`ai_providers._log_cache_plan` logs the decision and its reason once per
attempt. `input_tokens` is unchanged as a grand total, so existing readers are
unaffected.

## 2. Hybrid catalog tier (lever 1, applied to tools)

The obvious move — stub everything, let the model re-request — is wrong here,
and the existing code comments say why: jarvis is a fresh process per call, so
a re-request costs a full extra round trip (the whole prompt, resent), not a
cheap in-session lookup.

So: hybrid. `route.matches` already records which tool each qualifying keyword
hit. Those get their **full** schema and stay callable with no extra round
trip; the rest of the group drops to a ~10-token catalog line plus the new
`get_tool_schema` meta-tool (the `defer_loading` / fetch-schema step), which
promotes a tool through the existing `_discover_sink`.

Measured: playnite 2,699 → 1,727 tok (36%), desktop 1,035 → 785 (24%).
No-ops below `CATALOG_TIER_MIN_TOOLS` (10) and in precise mode, which opts out
because maximum schema fidelity is its whole purpose.

## 3. Skills (lever 1, as designed)

`skills.py` implements the three tiers literally, over `~/.jarvis/skills/<slug>/SKILL.md`:

- **Tier 1 (discovery)** — name + description only, one line per skill.
  Injected into the **static** half of the system prompt, so it sits *inside*
  the cached prefix. Installing ten skills costs ~200 tokens per cache write,
  not per request. Zero tokens when none are installed.
- **Tier 2 (activation)** — the SKILL.md body, only on `load_skill`. Clipped
  at ~5k tokens with a warning rather than rejected.
- **Tier 3 (execution)** — one reference file at a time, on request.
  `read_reference` **refuses to read scripts** and points at running them
  instead. That refusal is the whole tier-3 saving: without it, "reference"
  and "script" collapse and a skill can no longer ship code for ~0 token cost.
  Path containment is enforced by resolving both sides, not by string-matching
  `..` (a test pins that a path normalizing back inside its own folder is
  correctly allowed).

Six tools: `list_skills`, `load_skill`, `load_skill_reference`,
`create_skill`, `add_skill`, `remove_skill` — in their own `skills` router
group, with `remove_skill` confirm-gated.

Six CLI commands (`skills-list/get/save/add/create/remove`) and REST endpoints
at `/api/skills`. The web manager goes through these rather than
`/api/tools/run`, because it's a person editing their own files: it must not
inherit the model-facing confirm gate, and raw SKILL.md read/write is
deliberately *not* exposed as a model tool — handing the model arbitrary
overwrite on its own instruction set is a bad idea however convenient.

**Web UI** — a Skills button next to Debug opens a manager with an installed
list, a SKILL.md editor, and create/import panes. The header shows the live
catalog token cost, because that number is the entire argument for the design
and is the only cost that grows with skills-you-have rather than
skills-you-use. Invalid skills stay visible with their reason attached; a
skill the user thinks they installed but that silently never loads is the
worst failure mode this feature could have.

Format is Anthropic's Agent Skills standard, so skills written for Claude,
Copilot or Cursor import as-is.

## Bug found and fixed

`_record_usage("openai_compatible", ...)` was called **twice** per round in
`call_openai_compatible`, double-counting every OpenAI-family round in the
usage summary, the debug panel, and the Logs viewer. Found while adding cache
accounting, which would have inherited the same doubling.

## Tests

`tests/test_prompt_cache.py` (36) and `tests/test_skills.py` (55), in the
existing suite's style. They assert on prefix *stability* and byte-identical
fallbacks rather than on "a `cache_control` key appears", since the former is
what actually breaks if someone later reorders the prompt.

Full suite after the change:

```
test_dev_agent.py             9 passed      test_prompt_cache.py      36/36 passed
test_dev_agent_errors.py     11 passed      test_schemas_for_tools.py 14/14 passed
test_dev_agent_sandbox.py    12 passed      test_skills.py            55/55 passed
test_tool_context.py          5 passed
```

Three files still fail: `test_dev_agent_events.py`,
`test_enhancements.py`, and 4 cases in `test_provider_override.py`. All three
fail **identically on the pristine upload** — verified by extracting the
original zip and running them there. They are pre-existing and untouched by
this work.

## Config

New keys under `defaults`, all overridable per provider, documented in
`ai_config.py`'s docstring:

```
prompt_cache            true     master switch
prompt_cache_ttl        "5m"     or "1h" (Anthropic)
prompt_cache_tools      false    Anthropic: extra breakpoint on tools
prompt_cache_key        true     OpenAI/Groq family: routing hint
gemini_explicit_cache   false    cachedContents API vs free implicit
ollama_keep_alive       "30m"    model residency
```

## Worth knowing

- Caching is **not** verifiable in this container — no network. Everything
  here is verified at the payload-construction and policy level; the
  cache-hit rates themselves need a live key. `cache_read_tokens` in the debug
  panel is the number to watch on the first real run.
- The 4,096-token floor on `claude-haiku-4-5` is tighter than it looks.
  Compact mode clears it; ultra mode does not. If caching matters more than
  per-ask size, a 1,024-floor model (Sonnet 4.5) caches in every mode.
