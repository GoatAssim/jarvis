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
    conversations.py      on-disk conversation history
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
        outbound.py       DM the owner

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
        channel_people.py     NEW — remember_sender / who_am_i_talking_to
        workspace_tools.py    NEW — daemons, log search, backlog as AI tools
        path_tools.py         NEW — move/copy/rename/make_dir/delete-to-trash
                              (`files` group; confirm-gated via TOOL_CONFIRM_REQUIRED)

web/
    server.js             Express + WS; shells out to `jarvis` for everything
    public/index.html     the whole UI
    public/app.js         the whole front-end
    public/style.css      theme + layout, including the classic/focus switch
    public/custom-tools.js       Custom Tools panel
    public/test-checklist.js     Menu -> Test Checklist (UI, browser-only results)
    public/test-checklist-data.js  the SHIPPED checklist catalogue: an entry per
                                 tool that ships with jarvis. Edit it whenever you
                                 add/rename/remove one. A tool module can carry its
                                 own entry instead (TEST_CHECKLIST) - see checklist_schema.py
    public/test-checklist.css    its styling (reuses the debug-* panel chrome)
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
Custom Tools, Channels, **Daemons**, **Backlog**, **Log search**, **Setup**.

Test Checklist is the one panel with no server route and no CLI command: its
catalogue is a static file plus any entries tool modules supply themselves, and
its results live in the browser's localStorage. The only request it makes is the
read-only `GET /api/tools` Debug already uses, which carries a tool's own
`checklist` / `checklist_group` when its module defined them.
See AGENTS.md -> "Test Checklist".

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
