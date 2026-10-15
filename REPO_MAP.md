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
   have been a global, that is why. (One caveat, and only for the web UI:
   `web/server.js` may run a request in a warm `jarvis _internal_bridge` worker
   instead of a new process -- see "The direct channel" in section 6. Plain CLI
   use, the scheduler and the gateways never do, and the rule above is still
   the one to code to: the worker is replaced when anything it loaded changes.)
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
    bridge.py             `jarvis _internal_bridge` (hidden): ONE warm process that runs
                          requests through the same `cli.main()` and talks to its caller over
                          typed line-delimited JSON on its own stdin/stdout. Only web/server.js
                          starts it; nothing in the CLI needs it. Wire, limits and the
                          staleness fingerprint are in its docstring. tests/test_bridge.py
    wire.py               the one-line seam the CLI uses to tell a host what it is printing:
                          `wire.trace(text, cat)` (stderr line + in-band category when hosted;
                          a plain stderr print otherwise) and `wire.reply(...)`. Inert without
                          a host; imports nothing
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
    reply_sanitize.py     degenerate-reply guard: a model reply that is mostly
                          zero-width/NBSP/ellipsis noise is cleaned to its complete
                          sentences, or emptied so the adapter's "empty response"
                          failover runs (tool_history kept). Normal replies pass
                          through unchanged. Called in all five adapters just
                          before their `if not text:` check.
                          tests/test_reply_sanitize.py
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
    conv_images.py        pictures kept WITH a conversation (~/.jarvis/conv_images): the Ask panel's and the ones people send on Discord / Instagram; ids, caps, delete
                          and unsplit; conversation/console views can be rebuilt from
                          it; `conv-export --raw` and `console-read` read it;
                          `replay_record()` is what the web UI replays
    thread_extras.py      the saved screenshot/download/... cards derived from a turn's
                          tool runs; used at write time and when rebuilt from the log
    history.py            (module docstring explains the process model)
    history_summarizer.py AI recap of older turns
    conv_search.py        search what was SAID
    prompt_cache.py       the static/dynamic prompt split
    settings_admin.py     L.43: the Settings > Advanced back end -- every rule for editing
                          ~/.jarvis files from the UI (secret masking, type/structure checks,
                          stale-write detection, backup-before-write, undo, guarded and
                          read-only groups); `jarvis settings-admin ...` (JSON on stdin).
                          Also the built-in defaults table (`_DEFAULT_SOURCES`), the
                          "changed from default" report (`defaults_report`) and the per-value
                          `reset_value` the Files tree uses (`defaults`, `reset` verbs)
                          Also covers the web console's `web/data/*.json` as `web-data/<name>.json`
                          (`_WEB_DATA_FILES` allow-list: three preference files editable, the rest
                          read-only; folder from `JARVIS_WEB_DATA`, else beside the checkout)
    tunables.py           L.43: the registry + store of owner-adjustable knobs (12 env
                          switches/constants that used to need a source edit);
                          `tunables.const()` / `get()` are how modules read them



    # --- doing things ----------------------------------------------------
    desktop_tools.py  screenshot_tools.py  ocr_tools.py  vision_tools.py
    file_tools.py     everything_tools.py  git_tools.py  pkg_tools.py
    web_tools.py      ytdl_tools.py        audio_tools.py  radio_tools.py
    spotify_*.py      playnite_*.py        json_tools.py
    clipboard_tools.py    clipboard_get / _set / _clear / _wait_for_change and clipboard_watch_get_pattern / _set_pattern (one OS dispatch per platform: PowerShell on Windows, pbcopy/pbpaste on macOS, wl-clipboard / xclip / xsel on Linux, with a clean "no display" capability check instead of a timeout); results capped at CLIPBOARD_MAX_CHARS. Spliced into CORE_TOOL_SCHEMAS / TOOLS by tools.py. Tests: `test_clipboard_tools.py`
    clipboard_cli.py      CLI handlers for the clipboard watcher (one dispatch branch in cli.py, as channels_cli.py does): `jarvis clipboard-watch` (the foreground worker the daemon supervisor execs; not run by hand) and `jarvis clipboard-watch-config [--pattern REGEX|--clear-pattern] [--poll-seconds N]`. Its docstring still calls the pattern a CLI-only knob; `clipboard_watch_set_pattern` has since been given to the model (owner decision D7)
    clipboard_watch.py    `jarvis clipboard-watch` (handled in clipboard_cli.py) -- the BUILT-IN `clipboard-watch` daemon (daemons.py BUILTIN, off until started): polls the clipboard and notifies on a change, optionally only when it matches a regex. A fixed Jarvis-owned argv, deliberately NOT a model-callable `daemon_add` (AGENTS.md invariant). Pattern is plain data in `~/.jarvis/clipboard_watch_config.json` (`jarvis clipboard-watch-config`, or the model via `clipboard_watch_set_pattern`); bursts are coalesced into one notification. Tests: `test_clipboard_watch.py`
    browser_tools.py      browser_goto / _click / _fill / _get_text / _screenshot / _wait_for / _close (master plan Part C): a stateful Playwright session with a persistent profile at `~/.jarvis/browser-profile` (logins survive). v1: one Chromium per `jarvis` process, closed at exit (atexit); v2: with `browser_warm_daemon` on and the daemon answering, calls go to it instead and fall back to the local session if it doesn't. Spliced in by tools.py. Tests: `test_browser_tools.py`
    browser_daemon.py     `jarvis browser-daemon` -- the BUILT-IN `browser` daemon (daemons.py BUILTIN, off until started): holds ONE warm Playwright session across asks and serves browser_tools' own `_local_tool_browser_*` functions over a loopback TCP socket (newline-delimited JSON, one request per connection; port in `~/.jarvis/browser-daemon.port`). Closes the browser, not itself, after `browser_daemon_idle_seconds` (default 600) of no use. Transport + lifecycle only: no second copy of the click/fill logic. Tests: `test_browser_daemon.py`
    command_tools.py  commands_config.py   saved user commands
    custom_tools*.py  user Python tools from ~/.jarvis/tools/ (custom_tools_suggest.py = the editor's inline suggestions, `ctools-suggest`; returns text only, never writes a tool). custom_tools_store.py also keeps DRAFTS: ~/.jarvis/tools/_drafts/<key>.json, backups of tools still being written (saved or not); custom_tools_ui_templates.py = the "Button page" / "Menu panel" templates and their starter html/js/css. custom_tools_store.py also owns a tool's WEB FILES (the folders its TOOL_UI declares): `web_footprint`, `read_web_file` / `write_web_file` (`ctools-files`, the editor's extra tabs), `import_bundle` / `read_zip_bundle` (`ctools-import`: several files, a folder or a .zip), `web_removal_plan` (`ctools-delete --folders`, `ctools-files removal`)
    tool_commands.py      TOOL_COMMANDS (I.2.11): validates the palette commands a tool file contributes (a typed twin of one of its own tools; no riskTier = dangerous) and builds the tool's arguments from typed words. `jarvis tool-commands list|prepare|run`; routes `/api/tool-commands*`. A command is off when the owner switches it off (Tool Manager) or when its tool is (`tools.command_off_reason`)
    tool_ui.py            TOOL_UI: validates a tool file's own screen (a button or Menu entry opening a page built from the tool's tool.html/js/css) and reads its files; paths can't leave the declaring file's directory. `jarvis tool-ui list|bundle|run`
    tool_maker.py        the Tool Maker conversation (L.53): the per-turn context (tool name + whole editor text, from a temp file server.js writes), WITHHELD_TOOLS (write-a-file / run-a-program tools it may not use), history trimming, the origin stamp. Active only while JARVIS_TOOL_MAKER_CONTEXT_FILE is set
    tool_disable.py       the owner's off switches (~/.jarvis/disabled.json): tools, saved commands, tool-registered personas, TOOL_UI screens, TOOL_COMMANDS palette words (`jarvis tool-disable-set <name> <true|false> --kind tool-command`). Daemons are NOT here (daemons.json `enabled`); a file whose tools are ALL off takes its daemons (tool_daemons.sync) and screens (tools.ui_is_off) with it (Q-L46m)

    # --- time, work and supervision --------------------------------------
    scheduler.py          jobs/reminders/watches; tick() is the heartbeat
    sched_daemon.py       a standing tick loop
    timespec.py           every time expression in the product
    tasks.py              long-running checkpointed work
    task_runner.py        one task step per process
    subagents.py          child agents with isolated API keys
    categories.py         L.11/L.14.3 category-name rules (trim, case-insensitive unique, 24 chars, 8 per item)
                          + rename_in_list / remove_from_list (rename, merge, delete a category as a whole)
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
        changelog.py      L.36-P15: the permission change log, one line per ACTUAL change in
                          `channels/changes.jsonl`. Written by the writers themselves
                          (config.add_to_set/remove_from_set/set_value[owner, allow_tools],
                          user_perms.set_tools/set_can_dm, people.set_follow, user_admin
                          set_handle/forget_person), so panel, quick setups, bulk edits and the
                          terminal are all covered. Names and switches only; never text or tokens
        servers.py        L.36-P7: the Discord servers + channels the bot is in (names only,
                          `channels/servers.json`, kept by the gateway) and per-server /
                          per-channel switches written ONLY into `scopes` of channels.json;
                          switches can only take access away. `view()` is the read model.
                          `_ids()` delegates to `config.normalize_entries` (a bare id where a
                          list belongs must never crash it)
        allowed_channels.py  L.36-P18: add/remove one entry on allowed_channels (panel + `channels-channels`); preview-first, never empties the list
        directory.py      @handle -> id, learned from real messages
        people.py         NEW — WHO a person is: name, notes, follow state, and (L.36) a
                          platform-given avatar URL (https + image-CDN allow-list only)
        user_perms.py     L.36: per-person permissions the three allow-lists can't say —
                          WHICH tools (an allow-list, never a deny-list) and whether Jarvis
                          may DM them, plus `tools_until` (L.36-P6: a deadline on a
                          tool grant; past it `resolve_tool_access` says no tools);
                          fails closed (`PermsUnreadable` -> tools off)
        person_memory.py  L.21/L.22: per-person facts, episodes and standing rules in
                          channels/people/<key>/memory.jsonl; relevance-ranked, budgeted,
                          one file per person (isolation is the file); `proposed` items never
                          reach the model until approved
        episodes.py       L.21 P4: summaries of a person's FINISHED conversations (6 h quiet),
                          written as `proposed` episode records that point at the transcript;
                          one small model call each, 20/day cap, background after a reply
        sender_gate.py    D-I10: the free pre-filter + one tiny yes/no call in front of
                          remember_sender; also decides which tools-off turns are offered it
        awaiting.py       L.23: threads the owner is waiting on; gates -> one classifier call ->
                          digest or notify_owner; log + feedback bias in channels/awaiting*.json
        thinking.py       L.44: /thinking + the per-person on/off(+lock) -> think_override; L.42.2: wants_trace() (owner only,
                          while "on") and format_trace() (Discord ||spoiler|| messages, Instagram one plain "Thinking:" message)
        approvals.py      L.29 / L.42.1: the chat confirmation channel -- owner-only Approve / Deny (Discord buttons, Instagram
                          whole-message Y / N), 60 s then denied, pending store cleared at gateway start, fail closed
        cheap_call.py     the one tiny history-free model call (injectable in tests)
        presets.py        L.36-P4: the quick setups (No access / Chat only / Chat + tell the
                          owner / Trusted) -- pure data, fixed in code, no I/O
        preset_admin.py   L.36-P4/P5: plan (preview) and apply one setup for one person, and
                          bulk_flag / bulk_preset for several. Only ever calls
                          user_admin.set_flag / set_tools; every refusal is per person
        allowed_guilds.py L.36-P17: add / remove one server on `allowed_guilds` (panel "Allowed servers" /
                          `channels-guilds`); the ONLY writer of that key. Add to an empty list or an unseen
                          id previews until confirmed; removing the last entry is always refused
        media_in.py       L.36-P8 / L.42.3 inbound: a picture a person sends on Discord or Instagram (or the owner attaches in
                          the Ask panel: `prepare_files`) -> a vision description for the model, only for the owner and
                          `image_allowlist`; CDN-host / size / count / hourly caps; the download is deleted right after the
                          vision call and a copy is kept via conv_images. The only reader of `image_allowlist`
        master_tools.py   L.36-P9: the platform-wide `allow_tools` switch for the panel / `channels-master-tools`:
                          view (who it would reach), turn ON only after a preview + confirm, OFF at once,
                          refused while the tool list holds "*"
        panel_dm.py       L.36-P13: one DM from the panel / `channels-send`, preview then confirm, through
                          actions/send_dm.tool_send_dm so the limits and log are shared with the tool
        denied.py         L.36-P14: recently turned-away senders (who-stages only; never message text)
        master_tools.py   L.36-P9: the platform-wide `allow_tools` switch for the panel/CLI --
                          view() of who it would reach, set_master() = preview, then
                          confirm; refused while the tool list holds "*"; OFF is immediate
        panel_dm.py       L.36-P13: one DM from the panel, preview then confirm, through
                          actions/send_dm.tool_send_dm (shared limits, log, delivery);
                          refuses owner / placeholder / chat-origin / unattended
        denied.py         L.36-P14: read-only "turned away recently" from the transcript
                          (who-stages only; handle, count, reason -- never message text)
        user_admin.py     L.36: one person, every switch (dm/reply/tool/image/owner/send_dm/blocked
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
        person_memory_tools.py  L.21-L.23 owner-only: person_remember/forget/instruct/recall/
                              review + await_reply (own `person_memory` group)
        workspace_tools.py    NEW — daemons, log search, backlog as AI tools
        path_tools.py         NEW — move/copy/rename/make_dir/delete-to-trash
                              (`files` group; confirm-gated via TOOL_CONFIRM_REQUIRED)

web/
    server.js             Express + WS; shells out to `jarvis` for everything
    bridge.js             the server's end of the direct channel: a small pool of warm
                          `jarvis _internal_bridge` workers. `request()` never blocks or
                          queues -- no idle worker means null and server.js starts a process
                          as it always did. tests/verify_bridge_client.js
    public/index.html     the whole UI
    public/app.js         the whole front-end (also defines window.JarvisHost, the
                          small facade slash-palette.js talks to it through)
    public/style.css      theme + layout, including the classic/focus switch
    public/tool-manager.js (+ .css)  Tool Manager panel (L.25): catalogue, per-tool safeguards, user tools; also lists daemons / tool-registered personas / TOOL_UI screens with an on/off switch, and "Unfinished tools" (draft backups)
    public/tool-ui.js (+ .css)       renders TOOL_UI screens: buttons beside Menu (sandboxed frame) and Menu entries (shadow-root panel), plus the `host` API they talk to; pure helpers in JarvisToolUI._pure
    public/ask-images.js             pictures in the Ask Jarvis composer (attach / paste / drop, limits, what a saved exchange draws); pure helpers in JarvisAskImages
    ask-images.js                    (server side) checks and stages the pictures the Ask panel sends: count, size, real image bytes, private temp folder
    public/ask-embed.js (+ .css)     an Ask Jarvis panel to put inside another screen (L.53): the main Ask panel's websocket protocol, one conversation per instance, small / normal layouts by the pane's own width; first host is the Tool Manager's side panel; pure helpers in JarvisAskEmbed
    public/code-editor.js (+ .css)   the Tool Manager's code editor (L.33): Python highlighting, VS Code-style editing, completions, inline Jarvis suggestions; pure helpers in JarvisCodeEditor._pure
    public/agent-panel.js (+ .css)   the Focus layout's Agent side panel: while dev_agent / code_agent runs it shows the codebase root, the file being worked on, a file tree with a status per file, and a short log. Fed by app.js from the existing JARVIS_MEDIA dev_agent events (no server route); reducer + tree builder in JarvisAgentPanel._pure
    public/custom-tools.js       theme gallery for the Skin modal (the old Custom Tools panel moved to tool-manager.js)
    public/settings.js (+ .css)      L.43: the Settings modal (replaced the Skin modal): section rail, search, AI / Layout / Notifications / Tools / Integrations / Memory / About panes and the Advanced tab (Files, Tunables, Plumbing, History); talks only to /api/settings/*; the Skin section stays app.js's
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
    public/log-conversation.js/.css  Menu -> Logs, "Conversation" view (L.2):
                                 build() turns flat log lines into turns of
                                 user bubble / assistant bubbles / tool cards /
                                 timeline rows (pure, vm-tested); render()
                                 draws them with the Ask panel's bubble classes.
                                 Raw JSON view still shows every line
    public/schedules.js/.css     Menu -> Scheduled (Part H.2 rework: full
                                 create form onto remind_me/notify_me/
                                 schedule_task/schedule_watch via /api/tools/run,
                                 kind/status filters, Overview pane)
    public/app-icon.js           L.48: browser-tab icon = the skin's logo in the skin's colour; a distinct
                                 "working" icon (turning arc + badge, in the skin's accent) while a run is in flight;
                                 transparent background (no tile)
    public/channels-panel.js/.css  Menu -> Channels (L.36 rework): per-person, Test-Checklist-style
                                 (people list with a pinned "Home" | Profile + Permissions tabs for a
                                 person, or for Home: Platforms / Servers / Turned away / Global
                                 lists tabs -- the old right-hand side panel). Every switch POSTs to
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
    public/category-manager.js   Manage categories dialog (L.12): rename / merge / delete a daemon (or command)
                                 category as a whole. Only the Daemons panel opens it now; the Commands panel
                                 moved to category-menu.js. Pure helpers in JarvisCategoryManager._pure.
                                 Routes: /api/daemon-categories, /api/command-categories (list, rename, delete).
    public/category-menu.js      Commands panel category dropdown (L.14 UI rework), under the search box: filters
    public/category-menu.css     the list (All / Uncategorized / one category, remembered in localStorage
                                 `jarvis.commands.category.v1`) and renames / merges / deletes a category in place
                                 through the same /api/command-categories routes. Counts come from the commands the
                                 page already holds. Replaces the old header "Categories" button + dialog chain.
                                 Pure helpers in JarvisCategoryMenu._pure. Created lazily by categoryMenu() in app.js.
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
| `tunables.json` | tunables.py | owner overrides for the registered tunables (written only by settings_admin) |
| `settings_backups/<file>/<stamp>.json`, `settings_backups/changes.jsonl` | settings_admin.py | the previous contents before each Settings save (newest 20 per file) and a log of what was changed (paths only, never values) |
| `<repo>/web/data/favorites.json`, `favorite-daemons.json`, `ask-console-filter.json` (gitignored; not under `~/.jarvis`) | web/server.js | starred commands, starred daemons, hidden Ask-trace kinds. Also editable from Settings > Advanced > Files as `web-data/<name>.json` (settings_admin.py repeats the server's rules; backups still go to `settings_backups/web-data__<name>/`). `reboot.json` in the same folder is shown read-only; nothing in this tree writes it |
| `conversations/` | conversations.py | chat history |
| `logs/<conv>.jsonl` | logs.py | model↔backend traffic |
| `events/<conv>.jsonl` | raw_archive.py | raw event log (user text, unsplit model replies, full thinking, every console line, full tool results); deleted with the conversation or on Clear; `JARVIS_RAW_ARCHIVE=0` stops writing |
| `commands.json` | commands_config.py | saved commands |
| `scheduler.json` | scheduler.py | jobs and reminders |
| `tasks/` | tasks.py | one file per long-running task |
| `channels.json` | channels/config.py | both platforms, all allowlists |
| `channels/directory.json` | channels/directory.py | @handle → id |
| `channels/people.json` | channels/people.py | who each person is (name, notes, follow state, avatar URL) |
| `channels/user_perms.json` | channels/user_perms.py | per-person tool scope + "Jarvis may DM them" + `tools_until` (L.36-P6 deadline, epoch seconds); only non-default values stored; unreadable = fail closed |
| `channels/people/<key>/episodes_done.json` | channels/episodes.py | L.21 P4: which conversations were already summarized (thread + first timestamp, no text); deleted with the person |
| `channels/episodes.json` | channels/episodes.py | L.21 P4: automatic on/off and today's summary-call count |
| `channels/people/<key>/memory.jsonl` | channels/person_memory.py | L.21/L.22: facts, episodes and rules about ONE person (+ `_anyone` rules); `instructions_applied.jsonl` logs which rule fired (ids only) |
| `channels/awaiting.json`, `awaiting_log.jsonl` | channels/awaiting.py | L.23: who the owner is waiting on, the notify bar, quiet hours, decision log (verdicts, not message text) |
| `channels/thinking_threads.json` | channels/thinking.py | L.44: a person's own `/thinking` choice per thread |
| `channels/pending_approvals.json` | channels/approvals.py | L.29 / L.42.1: approval prompts waiting for the owner -- id, platform, tool name, times; never the arguments; each platform's rows are dropped when its gateway starts |
| `channels/servers.json` | channels/servers.py | L.36-P7: `{platform: {guild_id: {name, left, seen, channels}}}` -- names only, a cache; losing it costs labels, not behaviour |
| `channels/usage.jsonl` | channels/usage.py | L.36-P2: one counts-only line per answered ask (who, when, tokens, tool names); rotated, never deleted |
| `channels/changes.jsonl` | channels/changelog.py | L.36-P15: one line per permission change (when, whose id/handle, what, `via` panel/terminal, optional `why`); rotated, never deleted |
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

Both `dev_agent` and `code_agent` print their progress as one
`JARVIS_MEDIA\tdev_agent\t<json>` line per step (`dev_agent_events.py`). The
Focus layout's Agent panel (`web/public/agent-panel.js`) is built purely from
those events: dev_agent's `plan:start` carries `project_dir`, code_agent's
carries `root`, and a code_agent `list_dir` result carries a capped `listing`
on the **live line only** — it is removed from the stored `steps` right after
`emit()` prints it. (`code_agent.py` carries its own `TOOL_RESULT_SPECS`, merged
in by `tools.py`: it drops each step's `arguments` at medium/low verbosity and
all `steps` at low, but at high `steps` reach the model verbatim, and `listing`
is not in that spec — so leaving it in would have fed it to the model.) Both
agents are saved as `devAgent` replay extras (`thread_extras.py`), so both reappear
in the panel after a reload. A code_agent run is saved **slimmed**
(`_slim_code_agent_steps`): its `start` events carry the model's whole tool
arguments, so only `path` / `command` / `pattern` / `glob` / line range survive
and file content never reaches the saved conversation. Consequence: a reloaded
code_agent tree shows only the files it read or changed, not the folders it browsed.

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
jarvis channels-history <platform> <id|@handle> [limit]         # every recorded change to their access, who/what/when/how (JSON, read-only)
jarvis channels-handle <platform> <id|@handle> <new-handle>     # L.36-P10: correct the handle of a hand-added person who hasn't written; their list entries move with it
jarvis channels-tools-for <platform> <id|@handle> <30m|24h|7d>  # L.36-P6: tool use for that long, then it ends by itself (1m..30d); refused for the owner / blocked / "*"-covered
jarvis channels-instruction <platform> <id|@handle> show|set <text ...>|clear   # L.36-P12: the owner's own line on HOW to talk to them (style only, <=240 chars); refused for the owner
jarvis channels-user-test <platform> <id|@handle> dm|group [mentioned|unmentioned]   # DRY RUN of the gate: no model, nothing sent or saved
jarvis channels-presets                                         # the quick setups (JSON)
jarvis channels-preset <platform> <id|@handle> <setup> [preview]   # none|chat_only|chat_notify|trusted for ONE person; `preview` writes nothing
jarvis channels-bulk flag <switch> <on|off> <platform:id> ...      # one switch for several people; refusals reported per person
jarvis channels-bulk preset|preview <setup> <platform:id> ...      # a quick setup for several people / what it would change
jarvis channels-servers [platform]                                # Discord servers + channels Jarvis knows, with their switches (JSON)
jarvis channels-server-set <platform> guild|channel <id> <enabled|tools|mention> <on|off|inherit>   # can only take access away
jarvis channels-guilds <platform> [add|remove <id>] [--yes]    # allowed_guilds: add previews unless --yes when it turns the filter on / id unseen; last entry never removable
jarvis channels-add-person <platform> <id|@handle> [name ...]   # someone who hasn't messaged yet; grants nothing
jarvis channels-rename <platform> <id|@handle> [name ...]       # no name clears it
jarvis channels-remove-person <platform> <id|@handle>           # hand-added and never messaged only
jarvis channels-note <platform> <id|@handle> list|edit <note-id> <text ...>|delete <note-id>   # a note is named by id (hash of its text), not position
jarvis channels-forget <platform> <id|@handle> [--history] [--yes]   # L.36-P11 wipe; no --yes = preview. Refuses owner / "*"-covered / unreadable limits
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

**The direct channel (`web/bridge.js` <-> `jarvis/bridge.py`).** The server used to start a
`jarvis` process for every ask, panel call and scheduler tick. It now keeps up to
`JARVIS_BRIDGE_MAX` (default 3) warm `jarvis _internal_bridge` workers and hands them
requests (`runJarvisOnce`, and `spawnAndStream` for asks and direct command runs), one
at a time per worker. Everything the old path did still exists and is the fallback:
no idle worker, a worker that is stale (jarvis source, `~/.jarvis/tools` or the MCP config
changed since it started), a command that needs a process of its own (`bridge.UNHOSTED`),
or a worker that dies before it runs anything -> the request is started as a process.
`JARVIS_BRIDGE=0` turns the channel off; `GET /api/bridge` shows the pool. The channel
carries typed events (`out`, `err` with the CLI's own `cat`egory, `stream`, `usage`,
`confirm`, `ui`, `reply`, `exit`), which the server turns into the same WebSocket messages as
before plus, additively, `cat` on `ask-stderr` and an `ask-reply` message that the browser does
not read yet. The browser's own text classifiers (`splitConsoleDump`, `askConsoleKindOf`)
are untouched.

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
| `test_channel_servers.py` | L.36-P7 + tool calls in the Conversation view: the server registry, switches that only take access away (checked against the REAL `permissions.decide`), `servers.view`, thread -> parent-channel routing, tool-call scrubbing/clipping and attribution to the first reply chunk, the CLI; and (S3) a bare-id `allowed_guilds` / `allowed_channels` no longer crashes `view` or the gate |
| `test_allowed_guilds.py` | L.36-P17: `allowed_guilds` editor -- add (preview-first when it turns the filter on or the id is unseen), remove (the LAST entry is refused even with confirm), checked against the REAL `permissions.decide`, one-key-only writes, change log, `channels-guilds`, and a static check of the `server.js` route |
| `test_channel_timed_and_instruction.py` | L.36-P6 / P12: the `tools_until` deadline (garbage reads as passed, resolver is read-only), grant refusals, `expire_due` + its change-log reason, manual switches / block / forget / `channels-allow|deny` ending a countdown, the gateway sweep, the Test tab's wording, and the instruction (cap, one line, owner refused, prompt framing, not reachable by a guest) |
| `test_channel_insights.py` | L.36-P1/P2/P3: per-person conversation attribution (DM vs group, rotated files, torn lines), the usage ledger (counts only, failed asks, windows, rotation), and `simulate` (agrees with what `handle_message` really hands the model; writes nothing, calls no model — snapshot of `~/.jarvis` before/after) |
| `test_channel_media_out.py` | L.42.3 outbound: `media_out` collector (no-op outside a chat ask, owner only, capped), `channels/media_send.prepare` (owner only, extension allow-list, size cap, shrink-or-say), screenshots reach the owner's Discord thread after the text and a guest's never do |
| `test_ask_images.py` | pictures kept with a conversation and in the Ask panel: `conv_images` store, Instagram hosts + DM parsing, `media_in.prepare` / `prepare_files`, `images` / `shown` through the exchange, raw replay, redo / delete / clear, the CLI manifest, limits agreeing with `web/ask-images.js` |
| `verify_ask_images.js` (`node`) | the panel's picture helpers (limits, picture URLs, what a reload draws), the server's picture checking + staging, and the app.js / server.js / page wiring |
| `test_channel_media_in.py` | L.42.3 inbound / L.36-P8: `channels/media_in` (what counts as a picture, CDN-host-only urls, size / count / hourly caps, magic-byte check, description wrapped as content), the picture grant read through `handle_message` (owner and listed guests read, others told, denied senders never fetched), the Discord gateway's descriptors, `vision_call.load_image` reporting the real image type |
| `verify_l53_tool_maker_ui.py` (`python3`) | L.53: the Tool Maker's Ask Jarvis side panel in a real browser (toggle needs an editor, one conversation per tool tab, New chat asks first, Undo, Tool Maker tag); SKIPs without playwright + Chromium |
| `test_channel_pack_p8_p9_p13_p14.py` | L.36-P8/P9/P13/P14: the picture grant (never a way into a conversation, cleaned up by block / forget, owner locked), the platform tools switch (preview then confirm, refused with `"*"`), panel DM (shared limits, owner / chat-origin refused, preview sends nothing), the turned-away list (who-stages only, never message text) |
| `test_chat_approvals.py` | L.29 / L.42.1 + L.42.2: the confirmation channel (owner-only, 60 s timeout, restart, late / repeated / guest presses, Instagram Y / N, Discord buttons against a stand-in, action shown in full or not asked) and the thinking trace in chat (owner only, spoiler / plain format, bounds), both through `handle_message` |
| `verify_thinking_row.js` (`node`) | L.44 panel row: the Thinking control in Permissions > Capabilities (choices, lock box, owner has none, the POST each press makes) against a DOM stand-in |
| `verify_l36_pack_ui.py` (`python3`) | L.36-P8/P9/P13/P14/P16 panels + the add-person panel in a real browser against real backend output (`_channels_pack_fixture.py`); SKIPs without playwright + Chromium |
| `verify_l36_guilds_ui.py` (`python3`) | L.36-P17 in a real browser against real backend output: Home's Platforms tab shows the status cards, the Servers tab scrolls 40 servers inside its own box, the status dropdown, and the allowed-servers editor (preview, cancel, confirm, last entry locked); SKIPs without playwright + Chromium |
| `verify_l36_insights_ui.py` (`python3`) | L.36-P1/P2/P3 tabs in a real browser against real backend output (`_channels_fixture.py`); SKIPs without playwright + Chromium |
| `verify_channels_panel.js` (`node`) | L.36: `channels-panel.js` pure helpers (initials, hue, relative time, list filters and search, tool-scope diffing/grouping, and that only fixed icon strings reach `innerHTML`) |
| `test_prompt_cache.py` | the static/dynamic prompt split |
| `test_schemas_for_tools.py` | router ↔ catalog consistency |
| `test_dev_agent_sandbox.py` | path escapes, dependency validation |
| `test_scheduler.py` / `test_timespec.py` | jobs and time parsing |
| `test_h3_creation_confirmation.py` | H.3: `remind_me` / `schedule_task` / `schedule_watch` / timed `notify_me` send one `scheduled`-kind confirmation (not for immediate `notify_me`; `confirm: false` suppresses; level from `levels.scheduled`, not the job's own) |
| `test_l16_caps_and_budget.py` / `test_l16_scheduler_budget.py` / `test_l16_replay.py` | L.16 items 7, 8, 10: `list_windows` cap, per-ask token ledger and budget, per-job limit + "over budget" status, and the incident `1a99e1e3f0d3af0e` replayed through the real `ask()` (fixture: `tests/fixtures/1a99e1e3f0d3af0e.jsonl`) |
| `test_l24_fixes.py` | L.24 T2/T3/T4/T7: the catalog tier's net-saving gate, `get_tool_schema` list form and exact promotion (`OrderedSchemaSet.replace`), Gemini dict-result compaction, the interactive sensing-failure hint, and the failover note's "do not run them again" line |
| `test_l24_t5_static_text.py` | L.24 T5: the skills catalog's compact/ultra header and description clip (full mode unchanged), the shorter compact tools blurb, and that the desktop workflow note appears only when the desktop group is active |
| `test_l24_discovery_retry_cap.py` | L.24 round 2: the discovery-round metric (`RoundBudget.discovery_rounds` -> the per-ask total), the provider's retry hint on a 5xx (`[retry in Ns]`) and `key_health` parking an overloaded model for the STATED delay, and the interactive token limit (`defaults.interactive_token_budget`, 150,000 unless set; 0 turns it off); also pins that D5's same-key wait stays 429-only |
| `test_thinking_off_and_interactive_cap.py` | Thinking at Off and the interactive cap's default: `reasoning.gemini_off_patch` (a zero `thinkingBudget` for Gemini 2.5 Flash / Flash-Lite only; provider key `gemini_off_budget`), gpt-oss's Off effort override (`off_reasoning_effort`) and the once-per-ask floor note in the Logs viewer, and the 150,000 interactive default |
| `test_l24_5267_replay.py` | L.24 T1 regression: the real "has anyone texted you on discord" incident (`5267d91a8b996d48`, 5 rounds / 10,649 input tokens) from the scrubbed `fixtures/5267d91a8b996d48.jsonl`: asserts what the recording says, then runs the same words through the real `ask()` with a SYNTHETIC model reply and checks the current tool offering keeps the ask to 2 requests and an ESTIMATED 2,500 input tokens or less (it cannot test that the model picks `recent_dms`) |
| `test_l16_preflight_authorization.py` | L.16 round 2: `preflight.py` (an unattended ask that states a need the PC can't meet stops before any model call; fails open), the scheduler reporting that as a failure, the `message` kind in `job_risk`, `scheduler._unattended_tool_env` (a tool job now runs as unattended), and `send_dm`'s one exception to "never unattended" |
| `test_notification_inbox.py` | L.30: owner read/acknowledge state vs delivery (`seen_by`), mark read/all, dismiss, clear read, `summary()`, history filters, state-aware pruning (read first, unacknowledged last), the inbox lock, the `notify-*` verbs |
| `test_mcp_edit.py` | L.31: editing `mcp_config.json` (`save_server` / `set_server_flag` / `remove_server`), validation, secrets kept on edit, a malformed config is never overwritten, the extra `status()` fields and states, the `mcp-edit` CLI verb, and that no model tool can reach the edit functions |
| `verify_mcp_servers.js` (`node`) | L.31: `mcp-servers.js` pure helpers (state labels, search by tool name, filter chips, `buildSpec` and what a blank secret means) |
| `verify_mcp_servers_ui.py` (`python`, not run by `run_tests.py`) | L.31: the real page in headless Chromium against the real CLI verbs and a real stdio MCP server — add/edit/rename/remove, on/off, Trusted confirm, secrets never in the page, search/filters, keyboard, phone width |
| `verify_notifications.js` (`node`) | L.30: `notifications.js` pure helpers (burst collapsing, day sections, query string, read/ack readers, level badges) |
| `verify_notifications_ui.py` (`python`, not run by `run_tests.py`) | L.30: the real page in headless Chromium against the real CLI verbs — toast stack, level 5 dialog, panel filters/dismiss/mark-read, re-surfacing on a fake clock. Needs Playwright; skips cleanly without it |
| `test_category_manage.py` | L.12: rename / merge / delete a category -- list helpers, daemon registry (built-ins and tool-supplied included), the `daemon-category` verb, registration everywhere a verb must be, routes and wiring pinned statically |
| `verify_command_category_menu_ui.py` (`python`, not run by `run_tests.py`) | L.14 UI rework: the Commands category dropdown in headless Chromium against a stateful fake of the routes - header no longer overflowing, filter + search, saved / vanished choice, inline rename / merge / delete and every refusal, server error, keyboard, markup-looking names, Find box, short / narrow / phone layouts. Needs Playwright; skips cleanly without it |
| `verify_category_manager.js` (`node`) | L.12: `category-manager.js` pure helpers and the whole dialog chain (pick, rename, merge confirm, delete, refusals, empty list) against a fake JarvisUI and fetch |
| `test_l27c_autostart_notice.py` | L.27 follow-up (C5): a failed daemon autostart raises one `daemon` notification per pass -- text builder (singular / plural / 8-line cap), exactly one `notify()` for several failures, none for a clean pass, a raising notifier does not change `autostart_all()`'s result, the real inbox record and its level, and the Settings row |
| `test_l43_tree.py` | L.43: `default_doc`, `defaults_report` (changed / missing / extra, masked secrets), `reset_value` (backup, log, stale, guarded, unattended, wrong-type repair), CLI verbs, routes and wiring |
| `test_l43_web_data.py` | L.43: Settings > Advanced > Files covers `web/data` -- name/path rules, folder lookup (`JARVIS_WEB_DATA`), allow-list and read-only files, the server's rules repeated and pinned against `web/server.js`, backups/stale/unattended gate, CLI end to end |
| `test_l22_measure_home.py` | L.22: `measure_l22_paraphrase.py` -- `--live` gets the provider config in its sandbox HOME (only `ai_config.json`, deleted at exit), missing-fixture message, answered-call count and the no-answer warning |
| `verify_settings_tree.js` (`node`) | L.43: `settings.js` tree helpers (build, filter, leaf input), the `#settings/...` deep-link parser, and `/config` wiring |
| `test_slash_coverage.py` | every `reserved_names.py` name is in the `/` palette registry exactly once, with a valid risk tier |
| `verify_slash_palette.js` (`node`) | the palette engine: parsing, submit routing, near-miss, every verb's handler, confirm gates, keyboard model |
| `verify_slash_new_verbs.js` (`node`) | the `/search`, `/export`, `/remind` and `/persona` verbs: the reminder time/message splitter, each handler's request and failure paths, the persona and format argument lists |
| `test_allowed_channels.py` | L.36-P18: the `allowed_channels` editor -- preview-first, last entry never removed, real `permissions.decide` gate (thread = parent, DMs untouched, server filter warning), only that key written, change log, CLI, route + panel wiring |
| `test_bridge.py` | the direct channel's CLI half, against a REAL worker on a throwaway HOME: ready/protocol, one process serving many requests, env overlay applied then restored (grandchild output captured), request stdin, confirm round trip (yes and no), typed `reply` next to the stdout line, `cat` on trace lines, `refuse` / `retire` only before `started`, the staleness fingerprint, and that the CLI never imports or needs the bridge |
| `test_skillunload_unknown.py` | I-B3 / C10: `skill_stickiness.unload()` return value and `is_loaded()` (exact scope), and `jarvis skillunload` end to end -- loaded name unloads, unknown name / second unload / conversation unload of a global skill exit 1 with an explanatory error and leave the store untouched, `--all` and usage unchanged |
| `test_c29b_desktop_deps.py` | C29b: `desktop_tools` still loads when pygetwindow / pyautogui raise something other than ImportError; `jarvis doctor` has a `pygetwindow` row (skip, Windows-only note and no pip advice elsewhere); every desktop tool has a `tool_diagnosis._TOOL_DEPS` entry and its not-installed error names the right pip package |
| `verify_bridge_client.js` (`node`) | `web/bridge.js` against a fake worker: never blocks/queues, reuse, confirm round trip, `refuse`/`retire`/death-before-start fall back, death after start is a crash not a re-run, kill, breaker after three failed warm-ups, recycling without cutting off a chained request; no npm install |
| `verify_app_icon.js` (`node`) | L.48: `app-icon.js` pure helpers and a fake-DOM run (skin/colour change picked up, busy swaps the icon, ticker stops with Animations off) |


---

## 8. Invariants

- **Never drive-by edit** `tool_safety.py`, the confirmation prompts,
  `risk_review()`, or the AI-review gating in `_make_tool_executor()`.
- `MAX_TOOL_ROUNDS` stays 5. Optimize what is sent *per round*.
- `TOOLS` / `CORE_TOOL_SCHEMAS` stay fully loaded and locally executable.
  Only what is **sent to the model** is filtered.
- Per-turn content never enters the cached static prompt prefix.
- No module-level mutable global as a persistence mechanism — it will be
  empty on the next invocation. (Under the web UI's warm worker it would NOT
  be empty, which is a different way to be wrong: see the next item.)
- **Anything a process used to do at exit must be listed in
  `bridge.Host._end_of_request`** (today: MCP sessions, the browser a tool
  opened, an Everything.exe jarvis launched), and anything read once at import
  must be covered by `bridge.source_fingerprint`, or a warm worker will keep
  using it after it changed. `_internal_bridge` is a hidden subcommand: a
  request the worker cannot or should not host is answered `refuse`, never run.
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
