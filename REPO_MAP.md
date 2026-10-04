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
    conversations.py      on-disk conversation history (a derived view: newest 60 exchanges)
    raw_archive.py        L.38: the raw event log -- what actually happened, uncapped
                          and unsplit; conversation/console views can be rebuilt from
                          it; `conv-export --raw` and `console-read` read it;
                          `replay_record()` is what the web UI replays
    thread_extras.py      the saved screenshot/download/... cards derived from a turn's
                          tool runs; used at write time and when rebuilt from the log
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
    custom_tools*.py  user Python tools from ~/.jarvis/tools/ (custom_tools_suggest.py = the editor's inline suggestions, `ctools-suggest`; returns text only, never writes a tool)

    # --- time, work and supervision --------------------------------------
    scheduler.py          jobs/reminders/watches; tick() is the heartbeat
    sched_daemon.py       a standing tick loop
    timespec.py           every time expression in the product
    tasks.py              long-running checkpointed work
    task_runner.py        one task step per process
    subagents.py          child agents with isolated API keys
    categories.py         L.11/L.14.3 category-name rules (trim, case-insensitive unique, 24 chars, 8 per item)
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
        transcript.py     per-thread logs + cooldown state. L.36-P1: outbound lines carry
                          `to_user`, and `read_person()` assembles one person's slice of the
                          thread files (a rotated copy counts as its thread; an older group
                          reply with no `to_user` is attributed to nobody, never guessed)
        usage.py          L.36-P2: per-person usage ledger, one counts-only line per answered
                          ask in `channels/usage.jsonl` (tokens, requests, tool NAMES; never
                          text). Tokens as the provider counted them, no prices
        directory.py      @handle -> id, learned from real messages
        people.py         NEW — WHO a person is: name, notes, follow state, and (L.36) a
                          platform-given avatar URL (https + image-CDN allow-list only)
        user_perms.py     L.36: per-person permissions the three allow-lists can't say —
                          WHICH tools (an allow-list, never a deny-list) and whether Jarvis
                          may DM them; fails closed (`PermsUnreadable` -> tools off)
        presets.py        L.36-P4: the quick setups (No access / Chat only / Chat + tell the
                          owner / Trusted) -- pure data, fixed in code, no I/O
        preset_admin.py   L.36-P4/P5: plan (preview) and apply one setup for one person, and
                          bulk_flag / bulk_preset for several. Only ever calls
                          user_admin.set_flag / set_tools; every refusal is per person
        user_admin.py     L.36: one person, every switch (dm/reply/tool/owner/send_dm/blocked
                          + tool scope); the ONE implementation behind the panel and
                          `jarvis channels-user`; registered people only. L.36b adds
                          add_person / rename / remove_person / link_accounts and the
                          allow-list reconcile (sync_listed) run on every list_view.
                          L.36-P1/P2/P3 add three READ-ONLY views: conversation_view,
                          usage_view and simulate (the "Test as this person" dry run:
                          calls permissions.decide + user_perms.resolve_tool_access, the
                          same functions a real message uses; writes nothing)
        dedupe.py         NEW — has this message id already been handled
        outbound.py       DM the owner (dm_owner) or one known person (dm_person, L.20)

    # --- observability ----------------------------------------------------
    logs.py               structured conversation log entries + search
    log_files.py          NEW — greps the raw log FILES, line by line
    doctor.py             health checks and the fix for each
    onboarding.py         NEW — guided setup; the same steps in CLI and web
    turn_trace.py  token_usage.py

    # --- command surfaces -------------------------------------------------
    channels_cli.py       channels-* (incl. channels-users/-user/-user-tools, L.36; -add-person/-rename/-remove-person/-link/-unlink, L.36b), discord-daemon, instagram-serve
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
    public/code-editor.js (+ .css)   the Tool Manager's code editor (L.33): Python highlighting, VS Code-style editing, completions, inline Jarvis suggestions; pure helpers in JarvisCodeEditor._pure
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
    public/channels-panel.js/.css  Menu -> Channels (L.36 rework): per-person, Test-Checklist-style
                                 (people list | Profile + Permissions tabs | platform cards and the
                                 old global allow-lists as a fallback). Every switch POSTs to
                                 /api/channels/people/...; app.js only calls JarvisChannels.open().
                                 L.36-P4/P5: a Quick setups row (preview, then Apply) on the
                                 Permissions tab, and "Select people..." for a bulk editor
                                 (/api/channels/bulk, /api/channels/people/:p/:id/preset).
                                 Pure helpers in JarvisChannels._pure. Tokens never pass through it.
    public/mcp-servers.js/.css   Menu -> MCP Servers (L.31 rework): server cards with
                                 on/off + Trusted switches, per-server Refresh,
                                 Add/Edit/Remove form, tool lists, search and
                                 filter chips. Talks to /api/mcp and the
                                 /api/mcp/server routes; app.js only opens it.
                                 Pure helpers in JarvisMcp._pure.
    public/notifications.js/.css Notifications (L.30 rework): the toast stack, the
                                 inbox panel (read/unread, filters, search, burst
                                 collapsing, dismiss), the unread badge and the
                                 acknowledgment rules for levels 3-5. app.js only
                                 calls JarvisNotifications.show()/open(). Pure
                                 helpers exported as JarvisNotifications._pure.
    public/category-input.js/.css  L.11/L.14.3: the ONE category component - name rules (same as
                                 jarvis/categories.py, parity-tested), vocabulary, prefix suggest, chip input.
                                 Loaded before daemons.js; commands (L.14) will reuse it.
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
| `events/<conv>.jsonl` | raw_archive.py | raw event log (user text, unsplit model replies, full thinking, every console line, full tool results); deleted with the conversation or on Clear; `JARVIS_RAW_ARCHIVE=0` stops writing |
| `commands.json` | commands_config.py | saved commands |
| `scheduler.json` | scheduler.py | jobs and reminders |
| `tasks/` | tasks.py | one file per long-running task |
| `channels.json` | channels/config.py | both platforms, all allowlists |
| `channels/directory.json` | channels/directory.py | @handle → id |
| `channels/people.json` | channels/people.py | who each person is (name, notes, follow state, avatar URL) |
| `channels/user_perms.json` | channels/user_perms.py | per-person tool scope + "Jarvis may DM them"; only non-default values stored; unreadable = fail closed |
| `channels/usage.jsonl` | channels/usage.py | L.36-P2: one counts-only line per answered ask (who, when, tokens, tool names); rotated, never deleted |
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

**Per-person narrowing (L.36).** The gate above still decides *whether* someone
is answered and *whether* tools are allowed at all. Two things sit on top, and
both can only take access away: `user_perms.effective_tool_scope()` turns a
person's "custom" tool list into the `JARVIS_ALLOWED_TOOLS` allowlist for that
one ask (same variable, same lock as the tools-off case in `base._ask_jarvis`;
tools off always wins), and `user_perms.dm_allowed()` makes `send_dm` refuse a
person the owner switched DMs off for. An unreadable `user_perms.json` answers
that message with no tools (it never reads as "no limits"). The first three
panel switches (dm / reply / tool) are the same allow-list entries
`channels-allow` writes; the rest (owner, send_dm, blocked, tool scope) are
handled in `channels/user_admin.py`. A person covered by a `"*"` entry can't be
switched off individually — `set_flag()` refuses and says why.

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
jarvis notify-history [N] [--unread --failed --needs-ack --kind=K --source=S --q=words]
jarvis notify-summary    jarvis notify-read <ids|all>    jarvis notify-dismiss <ids|read|all>

# reachability
jarvis channels-status/set/allow/deny/test/whoami/log/directory
jarvis channels-people [platform] [--pending]
jarvis channels-follow|block <platform> <id-or-@handle>
jarvis channels-users [platform]                      # every registered person + every switch (JSON)
jarvis channels-user <platform> <id> <dm|reply|tool|owner|send_dm|blocked> <on|off>
jarvis channels-user-tools <platform> <id> inherit | custom [tool ...]
jarvis channels-conversation <platform> <id|@handle> [limit]    # what they sent and how Jarvis answered (JSON, read-only)
jarvis channels-usage <platform> <id|@handle> [days]            # their messages, tokens, tool calls (JSON, read-only)
jarvis channels-user-test <platform> <id|@handle> dm|group [mentioned|unmentioned]   # DRY RUN of the gate: no model, nothing sent or saved
jarvis channels-presets                                         # the quick setups (JSON)
jarvis channels-preset <platform> <id|@handle> <setup> [preview]   # none|chat_only|chat_notify|trusted for ONE person; `preview` writes nothing
jarvis channels-bulk flag <switch> <on|off> <platform:id> ...      # one switch for several people; refusals reported per person
jarvis channels-bulk preset|preview <setup> <platform:id> ...      # a quick setup for several people / what it would change
jarvis channels-add-person <platform> <id|@handle> [name ...]   # someone who hasn't messaged yet; grants nothing
jarvis channels-rename <platform> <id|@handle> [name ...]       # no name clears it
jarvis channels-remove-person <platform> <id|@handle>           # hand-added and never messaged only
jarvis channels-link <platform> <id|@handle> <other-platform> <id|@handle|name ...>   # same human; identity only
jarvis channels-unlink <platform> <id|@handle>
jarvis discord-daemon     jarvis instagram-serve

# MCP servers (a human action; no model tool can reach the edit path)
jarvis mcp-status/tools/refresh/call/config
jarvis mcp-edit <enable|disable|trust|untrust|remove> <name>
jarvis mcp-edit save <name> <json-definition> [--replace <current-name>]

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

### Code blocks and the one render path

Every reply drawn into an Ask bubble goes through **`renderRich(bubbleEl, markdown, { final, math })`**
in `app.js` (master plan Part I.1, I-B10): `renderMarkdown` -> `innerHTML` -> `linkifyPaths` ->
`renderMathIn` -> code-block chrome. The live stream preview, the pending bubble, a committed interim
bubble, `finalizeAskBubble` (both branches) and history replay all call it, so a live reply and the same
reply reloaded can't diverge. `final: false` marks text that may end mid-fence: the open fence is closed
for the preview only and that last block shows "writing..." with Copy disabled.

`web/public/rich-text.js` (+ `rich-text.css`, loaded before `app.js`, no dependency on it) owns the chrome:
`enhanceCodeBlocks` wraps each `<pre><code>` in a `.codeblock` (sticky bar with language, line count, Wrap,
Copy; collapse above 30 lines; highlight.js colouring when the language is known), `copyText` is the one
clipboard helper (`navigator.clipboard`, then an off-screen `<textarea>` fallback; the message Copy and the
selection pop-up use it too), and `renderUserText` renders fences in your own bubble (prose stays plain text).
Copy yields the block's `textContent` minus one trailing newline; only a shell block (bash/sh/shell/console/zsh)
whose every non-empty line starts with `$ ` loses that prompt, and the button says so. The buttons are built
after DOMPurify and are live only if `rich-text.js` created them (a `WeakMap`, not a class name), so
model-authored HTML can't forge one. highlight.js loads from a pinned CDN URL with `web/public/vendor/highlight.min.js`
as the offline fallback (`node web/scripts/vendor-highlightjs.mjs` fetches it; the version is read from `index.html`).

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
| `test_channel_manual_people.py` | L.36b: people named in an allow-list get a row, hand-added people, handle-only placeholder adopted on first message (tool limits migrated), locked names vs `remember_sender`, linked accounts (by id / handle / name, ambiguity, owner never inherited), remove, `send_dm` skips a handle-only person, the `channels-*` commands |
| `test_user_admin.py` | L.36: `user_perms` store (fails closed), `user_admin` switches (registered only, wildcard refusal, owner moves, block removes from all lists), the `base._ask_jarvis` enforcement point, the `send_dm` refusal, avatar validation in `people.py` |
| `test_channel_insights.py` | L.36-P1/P2/P3: per-person conversation attribution (DM vs group, rotated files, torn lines), the usage ledger (counts only, failed asks, windows, rotation), and `simulate` (agrees with what `handle_message` really hands the model; writes nothing, calls no model — snapshot of `~/.jarvis` before/after) |
| `verify_l36_insights_ui.py` (`python3`) | L.36-P1/P2/P3 tabs in a real browser against real backend output (`_channels_fixture.py`); SKIPs without playwright + Chromium |
| `verify_channels_panel.js` (`node`) | L.36: `channels-panel.js` pure helpers (initials, hue, relative time, list filters and search, tool-scope diffing/grouping, and that only fixed icon strings reach `innerHTML`) |
| `test_prompt_cache.py` | the static/dynamic prompt split |
| `test_schemas_for_tools.py` | router ↔ catalog consistency |
| `test_dev_agent_sandbox.py` | path escapes, dependency validation |
| `test_scheduler.py` / `test_timespec.py` | jobs and time parsing |
| `test_h3_creation_confirmation.py` | H.3: `remind_me` / `schedule_task` / `schedule_watch` / timed `notify_me` send one `scheduled`-kind confirmation (not for immediate `notify_me`; `confirm: false` suppresses; level from `levels.scheduled`, not the job's own) |
| `test_l16_caps_and_budget.py` / `test_l16_scheduler_budget.py` / `test_l16_replay.py` | L.16 items 7, 8, 10: `list_windows` cap, per-ask token ledger and budget, per-job limit + "over budget" status, and the incident `1a99e1e3f0d3af0e` replayed through the real `ask()` (fixture: `tests/fixtures/1a99e1e3f0d3af0e.jsonl`) |
| `test_notification_inbox.py` | L.30: owner read/acknowledge state vs delivery (`seen_by`), mark read/all, dismiss, clear read, `summary()`, history filters, state-aware pruning (read first, unacknowledged last), the inbox lock, the `notify-*` verbs |
| `test_mcp_edit.py` | L.31: editing `mcp_config.json` (`save_server` / `set_server_flag` / `remove_server`), validation, secrets kept on edit, a malformed config is never overwritten, the extra `status()` fields and states, the `mcp-edit` CLI verb, and that no model tool can reach the edit functions |
| `verify_mcp_servers.js` (`node`) | L.31: `mcp-servers.js` pure helpers (state labels, search by tool name, filter chips, `buildSpec` and what a blank secret means) |
| `verify_mcp_servers_ui.py` (`python`, not run by `run_tests.py`) | L.31: the real page in headless Chromium against the real CLI verbs and a real stdio MCP server — add/edit/rename/remove, on/off, Trusted confirm, secrets never in the page, search/filters, keyboard, phone width |
| `verify_notifications.js` (`node`) | L.30: `notifications.js` pure helpers (burst collapsing, day sections, query string, read/ack readers, level badges) |
| `verify_notifications_ui.py` (`python`, not run by `run_tests.py`) | L.30: the real page in headless Chromium against the real CLI verbs — toast stack, level 5 dialog, panel filters/dismiss/mark-read, re-surfacing on a fake clock. Needs Playwright; skips cleanly without it |
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
