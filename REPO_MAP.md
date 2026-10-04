# Jarvis — repository map

The one current map of this repo. Read it before grepping around.

Everything in `DOCUMENTATION/outdated/` is a point-in-time snapshot kept for
history and is **not** maintained — several of those files describe designs
that were never built, or bugs that were fixed years of commits ago. If a
statement there disagrees with this file, this file is right. The two docs
outside that folder (`DOCUMENTATION/PERSONAS_GUIDE.md` and the
`everything_sdk_*` / `yt-dlp-*` references) are still current: they document
external contracts rather than this codebase's own shape.

---

## 1. The shape of the thing

Jarvis is a local AI assistant with a large catalog of tools that act on the
machine it runs on. Three front-ends, one brain:

```
       CLI                Web UI              Chat gateways
  `jarvis ask …`      web/server.js        Discord / Instagram
       │                    │                       │
       └────────────────────┴───────────────────────┘
                            │
                      ai_client.ask()
                            │
        ┌───────────────────┼────────────────────┐
        │                   │                    │
  tool_router.route()  _build_messages()   ai_providers adapters
  (keyword, 0 tokens)  (prompt assembly)   (Gemini/OpenAI/Groq/…)
        │                                        │
        └──────────── active_schemas ────────────┘
                            │
                    _make_tool_executor()
                            │
                    tools.execute_tool()
```

Two facts explain most of the design:

1. **`jarvis` is a brand-new OS process on every CLI invocation.** Nothing
   survives in memory between calls. Every piece of state is a file under
   `~/.jarvis/`. Where you see an on-disk store that looks like it could
   have been a global, that is why.
2. **Sending the whole tool catalog on every message cost ~7.9k prompt
   tokens.** The router exists to send only the relevant slice. Everything
   in the "token optimization" family follows from that.

The long-lived exceptions to (1) are the daemons — the scheduler and the two
chat gateways — plus anything registered in `daemons.py`.

---

## 2. Package layout

Package root is `jarvis-cli/jarvis/`. Tests live in `tests/` at the **repo
root**, next to `jarvis-cli/`, not inside it.

```
jarvis-cli/jarvis/
    cli.py                argv parsing, chained commands, the ask entry point
    __main__.py           `python -m jarvis`

    # --- the brain -----------------------------------------------------
    ai_client.py          ask(), prompt assembly, the tool executor,
                          provider/key failover
    ai_providers.py       per-provider adapters + the tool-round loop.
                          finish_signal() normalizes each provider's own stop
                          field to finish "tool"|"done" + cut None/"length"/
                          "refused"/"filter" (Sec5); _surface_interim_text()
                          keeps + live-fires text sent alongside a tool call,
                          on all five adapters now. tests/test_finish_signal.py
    ai_config.py          ~/.jarvis/ai_config.json
    tool_router.py        keyword router — which tool GROUP does this need
    tool_registry.py      static: groups, keywords, per-group guidance
    tools.py              the full schema catalog + search_tools
    tool_loader.py        auto-discovery of actions/ and ~/.jarvis/tools/
    checklist_schema.py   shape + validator for a Test Checklist entry (shipped file and module-supplied alike)
    tool_safety.py        which tools need confirmation  (DO NOT drive-by edit)
    tool_result_shaping.py  trims a tool result before it goes back to the model
    tool_diagnosis.py     NEW — explains a FAILED tool call and names the fix.
                          read_screen/click_on_text have their own classifier
                          (_diagnose_ocr): it keys on the START of ocr_tools'
                          message (no PATH / no pip pkg / found-but-failed),
                          never on words inside it. tests/test_ocr_diagnosis.py
    tool_context.py …     (see ToolContext in tools.py)

    # --- memory and history ---------------------------------------------
    memory.py             durable facts, namespaced, relevance-scored
    memory_semantic.py    embedding/ngram similarity layer over the above
    memory_consolidation.py
    conversations.py      on-disk conversation history (newest 60 exchanges)
    raw_archive.py        L.38a: lossless per-conversation archive of what the
                          capped stores drop (overflow, clipped/trimmed console
                          lines, full tool results); `conv-export --raw` reads it
    history.py            (module docstring explains the process model)
    history_summarizer.py AI recap of older turns
    conv_search.py        search what was SAID
    prompt_cache.py       the static/dynamic prompt split

    # --- doing things ----------------------------------------------------
    desktop_tools.py  screenshot_tools.py  ocr_tools.py  vision_tools.py
    file_tools.py     everything_tools.py  git_tools.py  pkg_tools.py
    web_tools.py      ytdl_tools.py        audio_tools.py  radio_tools.py
    spotify_*.py      playnite_*.py        json_tools.py
    command_tools.py  commands_config.py   saved user commands
    custom_tools*.py  user Python tools from ~/.jarvis/tools/

    # --- time, work and supervision --------------------------------------
    scheduler.py          jobs/reminders/watches; tick() is the heartbeat
    sched_daemon.py       a standing tick loop
    timespec.py           every time expression in the product
    tasks.py              long-running checkpointed work
    task_runner.py        one task step per process
    subagents.py          child agents with isolated API keys
    daemons.py            NEW — registry + supervisor for every background
                          service, built-in or user-defined
    backlog.py            NEW — untimed work: idea/todo/doing/blocked/done
    ambient.py            NEW — notices problems without being asked
    digest.py  notifier.py  stats.py

    # --- reachability -----------------------------------------------------
    channels/
        __init__.py       platform ids and the four permission-set names
        config.py         ~/.jarvis/channels.json, both platforms
        permissions.py    THE GATE — pure functions, no IO, no SDK
        base.py           the pipeline every gateway runs
        discord_gateway.py     wire adapter (the only file importing discord.py)
        instagram_gateway.py   wire adapter (webhook server)
        transcript.py     per-thread logs + cooldown state
        directory.py      @handle -> id, learned from real messages
        people.py         NEW — WHO a person is: name, notes, follow state
        dedupe.py         NEW — has this message id already been handled
        outbound.py       DM the owner (dm_owner) or one known person (dm_person, L.20)

    # --- observability ----------------------------------------------------
    logs.py               structured conversation log entries + search
    log_files.py          NEW — greps the raw log FILES, line by line
    doctor.py             health checks and the fix for each
    onboarding.py         NEW — guided setup; the same steps in CLI and web
    turn_trace.py  token_usage.py

    # --- command surfaces -------------------------------------------------
    channels_cli.py       channels-*, discord-daemon, instagram-serve
    workspace_cli.py      NEW — daemons, log-file search, backlog, ambient,
                          onboarding, ui-mode

    key_health.py         NEW — ~/.jarvis/key_health.json: per-key 429 cooldowns, per-model
                          503 cooldowns, last-good key (ai_client.ask reorders, never removes)

    actions/              auto-discovered tool files (see _template.py)
        _template.py      the contract, commented field by field
        dev_agent.py      plan -> write -> install -> run -> fix, sandboxed
        code_agent.py  calendar_tools.py  mcp_tools.py  scheduler_tools.py
        conv_search_tools.py  notify_owner.py
        recent_dms.py         read-only, owner-only: who DMed the owner
        send_dm.py            NEW (L.20) — DM ONE known person; owner-only, confirm-gated,
                              rate-limited (~/.jarvis/channels/dm_sends.json), refuses unattended
        channel_people.py     NEW — remember_sender / who_am_i_talking_to
        workspace_tools.py    NEW — daemons, log search, backlog as AI tools
        path_tools.py         NEW — move/copy/rename/make_dir/delete-to-trash
                              (`files` group; confirm-gated via TOOL_CONFIRM_REQUIRED)

web/
    server.js             Express + WS; shells out to `jarvis` for everything
    public/index.html     the whole UI
    public/app.js         the whole front-end (also defines window.JarvisHost, the
                          small facade slash-palette.js talks to it through)
    public/style.css      theme + layout, including the classic/focus switch
    public/tool-manager.js (+ .css)  Tool Manager panel (L.25): catalogue, per-tool safeguards, user tools
    public/custom-tools.js       theme gallery for the Skin modal (the old Custom Tools panel moved to tool-manager.js)
    public/test-checklist.js     Menu -> Test Checklist (UI, browser-only results)
    public/test-checklist-data.js  the SHIPPED checklist catalogue: an entry per
                                 tool that ships with jarvis. Edit it whenever you
                                 add/rename/remove one. A tool module can carry its
                                 own entry instead (TEST_CHECKLIST) - see checklist_schema.py
    public/test-checklist.css    its styling (reuses the debug-* panel chrome)
    public/daemons.js/.css       Menu -> Daemons (Part H.1 rework: own file,
                                 same debug-*/menu-* chrome as Test Checklist)
    public/backlog.js/.css       Menu -> Backlog (Part H.1 rework: search,
                                 project filter, Overview pane, drag-and-drop
                                 between kanban columns)
    public/logsearch.js/.css     Menu -> Log Search (Part H.1 rework: live
                                 search, query highlighting, results/detail
                                 two-pane layout, jump-to-Ask/Daemons/Schedules)
    public/schedules.js/.css     Menu -> Scheduled (Part H.2 rework: full
                                 create form onto remind_me/notify_me/
                                 schedule_task/schedule_watch via /api/tools/run,
                                 kind/status filters, Overview pane)
    public/daemons.js            Menu -> Daemons (three-pane: services / selected service /
                                 overview; Console + Details tabs; add/edit form). Pure
                                 helpers are exposed as JarvisDaemons._pure for the node test
    public/daemons.css           its styling (own `dmn-` prefix, reuses the debug-* chrome)
    public/slash-commands-data.js  the "/" command palette's REGISTRY: every typeable
                                 verb (35) + a risk-tiered disposition for every other
                                 name in reserved_names.py. Edit it whenever you
                                 add/rename/remove a jarvis-cli subcommand - see
                                 AGENTS.md -> "The / command palette"
    public/slash-palette.js      the palette's engine + popover UI (parse, rank, complete,
                                 keyboard, submit routing, one handler per verb)
```

---

## 3. Where state lives

Everything is under `~/.jarvis/`:

| Path | Owner | What |
|---|---|---|
| `ai_config.json` | ai_config.py | providers, keys, prompt mode |
| `memory.json` | memory.py | durable facts |
| `conversations/` | conversations.py | chat history |
| `logs/<conv>.jsonl` | logs.py | model↔backend traffic |
| `archive/<conv>.jsonl` | raw_archive.py | uncapped copy of everything the stores above clip, trim or drop; deleted with the conversation or on Clear; `JARVIS_RAW_ARCHIVE=0` stops writing |
| `commands.json` | commands_config.py | saved commands |
| `scheduler.json` | scheduler.py | jobs and reminders |
| `tasks/` | tasks.py | one file per long-running task |
| `channels.json` | channels/config.py | both platforms, all allowlists |
| `channels/directory.json` | channels/directory.py | @handle → id |
| `channels/people.json` | channels/people.py | who each person is |
| `channels/seen_messages.json` | channels/dedupe.py | redelivery guard |
| `daemons.json` | daemons.py | the daemon registry |
| `daemons/<id>/` | daemons.py | console.log, status.json, stdin.queue |
| `backlog.json` | backlog.py | untimed work |
| `ambient.json`, `ambient_state.json` | ambient.py | monitor config + memory |
| `onboarding.json` | onboarding.py | setup state and the UI layout |
| `discovery_cache.json` | discovery_cache.py | recent search_tools hits |

---

## 4. Subsystems worth understanding before you touch them

### The router and the prompt
`tool_router.route(text)` scores keywords and returns groups. Confident →
only those groups' schemas are sent; not confident → just `search_tools` +
`search_commands`, and a hit grows the active set in place for the next
round. `MAX_TOOL_ROUNDS` is 5 and stays 5.

The system prompt is built in **two parts** (`_system_prompt_parts`): a
cacheable static prefix and a per-request tail. Anything that varies per
turn — memory context, the sender's identity, per-tool workflow notes, pack
instructions — **must** go in the tail. Putting per-turn text in the prefix
invalidates the whole cached block, which is a real bug that has been
introduced twice; `tests/test_prompt_cache.py` guards against it.

### The permission gate
`channels/permissions.py` is pure functions over `(config, message)`. Four
independent sets (`dm_allowlist`, `reply_allowlist`, `tool_allowlist`, plus
the `allow_tools` master switch), empty means deny, `"*"` means everyone.
Tool permission is computed independently of whether the message is
answered, so "answer them but touch nothing" is expressible.

Order matters: `reachable` is checked before any allowlist so a message that
was never addressed to us is dropped without being logged — and, since the
Discord fix, without being reacted to or typed at either.

### Identity on chat platforms
The system prompt assumes the owner is typing. Over Discord/Instagram that
is frequently false, so `channels/people.py` builds an identity block that
rides at the head of the per-request tail: who this is, that they are not
the owner, and to ask their name if unknown. A guest's details go in
`people.json`, never in `memory.py` — that store rides along in the owner's
own prompts, and letting a stranger write to it is one message away from a
problem.

### Daemons
Registry + supervisor. Starting one spawns a **detached supervisor**
(`jarvis daemon-run <id>`) which owns the child's pipes, pumps output to
`console.log` and drains `stdin.queue`. That indirection exists because a
third process cannot write to another process's stdin, and every `jarvis`
invocation is a third process.

Built-ins (scheduler, discord, instagram) are ordinary registry entries
whose argv points back at `jarvis`, so the custom-daemon path is the path
the built-ins use. A built-in's `argv`, `shell` and `supports_stdin` cannot
be edited: those decide what actually executes.

A **shell** daemon stores its command verbatim as a single element and is
never shlex-split, because splitting and re-joining destroys quoting.

### dev_agent
`plan → write → install → run → fix`, inside a sandbox
(`dev_agent_sandbox.resolve_within` rejects `../`, absolute paths and
symlink escapes). Python projects get their own venv. `dependencies` come
from a model and are validated against a package-name pattern before they
reach `pip`/`npm` — a leading-dash entry is a *flag*, not a package.

---

## 5. Command surface

```
# conversation
jarvis ask "…"            jarvis conv-new/list/show/switch/delete
jarvis mode / mode-set    jarvis ai-config / ai-clear

# what's wrong
jarvis doctor [--deep]    jarvis logs / logs-list / logs-search
jarvis logs-files <query> [--mode] [--set] [--path] [--context]
jarvis logs-tail <path>   jarvis logs-sets

# background services
jarvis daemons            jarvis daemon-start|stop|restart|status <id>
jarvis daemon-console <id> [--lines N] [--file PATH]
jarvis daemon-input <id> <text>      jarvis daemon-schedule <id> <when>
jarvis daemon-add <id> "<cmd>" [--name] [--cwd] [--stdin] [--env K=V]
       [--shell] [--restart never|on-failure|always] [--restart-delay S]
       [--max-restarts N] [--stop-signal TERM|INT|KILL] [--stop-timeout S]
       [--autostart] [--description D]
jarvis daemon-edit <id> [same flags]  jarvis daemon-remove <id>
jarvis daemon-run <id>    # the supervisor; runs in the foreground
jarvis daemons-tick

# work
jarvis backlog [--state S] [--project P] [--all]
jarvis backlog-add "<title>" [--project] [--state] [--priority]
jarvis backlog-done|update|remove <item>    jarvis backlog-board

# time
jarvis sched-list/add/cancel/snooze/approve/tick/daemon
jarvis notify-send/list/ack/clear

# reachability
jarvis channels-status/set/allow/deny/test/whoami/log/directory
jarvis channels-people [platform] [--pending]
jarvis channels-follow|block <platform> <id-or-@handle>
jarvis discord-daemon     jarvis instagram-serve

# monitoring and setup
jarvis ambient            jarvis ambient-tick
jarvis onboard [--json] [--skip]     jarvis ui-mode [classic|focus]
```

---

## 6. The web UI

`web/server.js` never reimplements a rule — every route shells out to the
matching `jarvis` subcommand, so the Python side stays the single source of
truth and keeps the tests.

Two layouts, switched from the topbar and persisted server-side via
`jarvis ui-mode`:

- **classic** — the Commands / Detail / Console grid, always visible. What
  this UI has always looked like.
- **focus** — the grid is hidden and the chat is the page.

The invariant that makes switching safe: **no feature exists in one layout
and not the other.** Focus only changes what is on screen by default;
everything is still in the same Menu. A feature reachable only in classic
would turn a presentation preference into a trap.

Panels: Guides, Debug, **Test Checklist**, Skills, Scheduled, MCP Servers,
Tool Manager (was Custom Tools), Channels, **Daemons**, **Backlog**, **Log search**, **Setup**.

Test Checklist is the one panel with no server route and no CLI command: its
catalogue is a static file plus any entries tool modules supply themselves, and
its results live in the browser's localStorage. The only request it makes is the
read-only `GET /api/tools` Debug already uses, which carries a tool's own
`checklist` / `checklist_group` when its module defined them.
See AGENTS.md -> "Test Checklist".

### The `/` command palette

Typing `/` at the start of the Ask box opens a floating, keyboard-driven
palette (master plan Part I.2). Level 1 lists the verbs grouped Chat / Model /
Skills / Run / Panels / View, each with a risk badge, a one-line summary and a
footer that says what Enter will do; Level 2 completes the verb's argument from a
live list (chats, saved commands, installed/loaded skills, providers, services,
capacity modes). It replaced the old `/skillload` / `/skillunload` regex + inline
suggest box - there is now exactly one place in the front-end that parses a
leading `/`.

- **Grammar.** Only a `/` at column 0 of a single-line message with no quote
  attached is a command; a second `/` in the first token (`/etc/hosts...`) or a
  leading `//` is prose (`//` sends the text with one slash stripped). Exact
  verb or alias only - a prefix never auto-runs on submit. An unknown verb within
  edit distance 2 is blocked once with "did you mean"; an identical second Enter
  sends it as a message.
- **Enter is two-step where a mistake would cost something.** A plain-`safe` verb
  with nothing required to fill in and outside the Chat group (panel openers,
  `/skills`, `/config`, `/help`...) runs on the first Enter. Everything else
  completes on the first Enter and runs on the second, so a half-typed `/cl` can
  never wipe a chat. Tab and a click only ever *fill*. Enter during IME
  composition is ignored.
- **Every reserved name is in exactly one of three places** in
  `slash-commands-data.js` (`tests/test_slash_coverage.py` enforces it):
  a verb's `covers` (reached through a typed verb - most are the plumbing a
  panel already drives), `passthrough` (runnable as `/<cli-name> [args]`: 31
  one-shot, headless commands such as `/doctor`, `/memory-recall`, `/logs-clear`),
  or `notExposed` (30 that cannot run from a chat box for a *technical* reason -
  a long-running supervisor, an interactive terminal flow, the microphone, an
  internal hook, a syntax token). Being destructive is not a reason to hide a
  command: it is `passthrough` with tier `dangerous`.
- **Passthrough is allowlisted twice.** The client sends `POST /api/slash/run
  {name, args}`; `server.js` re-reads the same `passthrough` table from the data
  file (one list, not two) and refuses any other name, then runs
  `jarvis <name> <args...>` as an **argv array, no shell**. `dangerous`-tier ones
  go through the same `JarvisUI.confirm` modal as `/clear`, showing the exact
  command line. Output is a toast if it is one short line, otherwise a dialog with
  the text (and the exit code if it failed).
- **Confirmations.** `/clear` and the toolbar Clear button share `clearAiChat()`,
  which confirms first (D-I7); `/daemon stop` always asks and `/daemon restart`
  asks when the service is actually up - the same rule the Daemons panel applies
  itself. `start` and `status` never ask.
- **Plumbing.** `slash-palette.js` fetches its own read-only lists over REST and
  goes through `window.JarvisHost` (bottom of `app.js`) for anything stateful -
  which chat is open, the websocket, opening a panel. Panels that live in their own
  file are opened through their own global (`JarvisSchedules`, `JarvisBacklog`,
  `JarvisLogSearch`, `JarvisDaemons`, ...). `GET /api/skills/loaded`
  (CLI: `jarvis skills-loaded [conv-id]`) exists only so `/skillunload` can offer
  the skills that are actually loaded. Recent use breaks ranking ties via a small
  MRU in `localStorage` (`jarvis.slash.mru`; verb ids and skill / saved-command
  names only, never chat titles).
- **Not built yet:** commands contributed by custom tools (I.2.11 - the plan says
  it needs its own tracking id and contract), copying an individual code block
  (`/copy 2` waits on Part I.1), and touch long-press for the hover tooltip (the
  footer describes the highlighted row; on touch there is no hover to move it).

---

## 7. Testing

No framework — plain `assert`, runnable directly:

```
python3 tests/test_workspace.py
python3 tests/test_channel_people.py
```

A new test file's `sys.path` must point at `jarvis-cli/`
(`Path(__file__).resolve().parent.parent / "jarvis-cli"`).

Anything touching `~/.jarvis` must redirect `HOME` to a temp dir **before**
importing jarvis modules — most of them resolve `Path.home()` at import
time. `tests/test_workspace.py` shows the pattern.

Define test functions **above** the runner block at the bottom of the file.
The runner reads `globals()` when it executes, so a test appended after it
silently never runs.

| File | Covers |
|---|---|
| `test_workspace.py` | daemons, log_files, backlog, ambient, diagnosis, onboarding |
| `test_channel_people.py` | identity, the gate split, dedupe, remember_sender |
| `test_channels.py` | the permission gate, config, transcripts |
| `test_prompt_cache.py` | the static/dynamic prompt split |
| `test_schemas_for_tools.py` | router ↔ catalog consistency |
| `test_dev_agent_sandbox.py` | path escapes, dependency validation |
| `test_scheduler.py` / `test_timespec.py` | jobs and time parsing |
| `test_h3_creation_confirmation.py` | H.3: `remind_me` / `schedule_task` / `schedule_watch` / timed `notify_me` send one `scheduled`-kind confirmation (not for immediate `notify_me`; `confirm: false` suppresses; level from `levels.scheduled`, not the job's own) |
| `test_l16_caps_and_budget.py` / `test_l16_scheduler_budget.py` / `test_l16_replay.py` | L.16 items 7, 8, 10: `list_windows` cap, per-ask token ledger and budget, per-job limit + "over budget" status, and the incident `1a99e1e3f0d3af0e` replayed through the real `ask()` (fixture: `tests/fixtures/1a99e1e3f0d3af0e.jsonl`) |
| `test_slash_coverage.py` | every `reserved_names.py` name is in the `/` palette registry exactly once, with a valid risk tier |
| `verify_slash_palette.js` (`node`) | the palette engine: parsing, submit routing, near-miss, every verb's handler, confirm gates, keyboard model |

---

## 8. Invariants

- **Never drive-by edit** `tool_safety.py`, the confirmation prompts,
  `risk_review()`, or the AI-review gating in `_make_tool_executor()`.
- `MAX_TOOL_ROUNDS` stays 5. Optimize what is sent *per round*.
- `TOOLS` / `CORE_TOOL_SCHEMAS` stay fully loaded and locally executable.
  Only what is **sent to the model** is filtered.
- Per-turn content never enters the cached static prompt prefix.
- No module-level mutable global as a persistence mechanism — it will be
  empty on the next invocation.
- There is **no debug/verbose flag**. All stderr trace output is
  unconditional.
- The model can start, stop and inspect daemons; it cannot create one.
  Registering a daemon stores a command Jarvis later runs unattended.

## Failure handling in ask() (master plan Part F)

- Tools withheld (budget spent) -> `ai_providers._forced_ending`: one optional grace round
  (`defaults.grace_call`, default on), then a flattened tool-less final request. Still wants a
  tool -> `KIND_BUDGET`: ask() stops rotating and returns `_forced_ending_reply` (harness-written).
- Failover carries the failed attempt's `tool_history` (`_carried_messages`); the old recap is
  only a fallback when there is no transcript.
- `AIResult.kind` (`KIND_*`, `classify_failure`) drives rotation; 503 and refused connections skip
  remaining keys / sibling hosts; key cooldowns live in `key_health.py`.
- Log entries for a request to a different host are labelled by host (`_log_provider_for`).
- Router: last line of a 3+ line, 200+ char message gets +2 (`tool_router.LAST_LINE_BONUS`);
  highlight-quote wrappers are stripped before routing (`_strip_highlight_excerpt`).
- Router keyword entries can carry `needs_any` (L.24 T1): the phrase counts only if one of
  the listed words also appears, so a bare platform word (`discord`) triggers `click` only
  with an action word. Used together with `not_with`; see `tool_registry.keyword_needs()`.
- Token accounting (L.24 T6): `ai_providers.get_usage_summary()` describes ONE attempt;
  `token_usage.AskUsage` (fed by `ai_client.ask()` after every adapter call, including
  failed attempts and same-key 429 retries) is the per-ask total, exposed as
  `AskResult.usage["ask_total"]`, the turn trace's `tokens`, a `logs` "info" row and the
  CLI/web token lines. Gemini `thoughtsTokenCount` is billed ON TOP of output and is added
  to totals; OpenAI-style `reasoning_tokens` are already inside output and are not.
  `tests/measure_l24.py` reprints the measurements behind these numbers.
