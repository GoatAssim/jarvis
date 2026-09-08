# Jarvis — Everything I Know

Personal AI-assistant: CLI + web console. Repo root `jarvis-main/`, two halves:
- `jarvis-cli/jarvis/` — Python backend/CLI (fresh process per invocation, no persistent session)
- `web/` — Node/Express server + vanilla JS/HTML/CSS frontend

**Workflow used across sessions:** unzip the uploaded repo, edit a working copy, generate a `git diff --no-index`-style `.patch` (paths as `a/path b/path`), verify `git apply --check` against a pristine copy, hand over a downloadable `.patch`. The repo gets re-uploaded repeatedly (jarvis-main_2, _3, ..._14...) as the user applies patches/makes own edits — never assume base state, always inspect the actual uploaded zip.

---

## jarvis-cli/jarvis/ — Python backend

- `cli.py` — entry point, argv dispatch. `handle_ai_prompt(text, commands)` = ask handler. `RESERVED_NAMES` = reserved subcommand names. Subcommands: `config`, `ai-config`, `ai-clear`, `ai-drop-from`, `tools-list`, `tool-run`, `tool-preview`, `tool-safety-set`, `conv-new/list/show/switch/delete`.
- `ai_client.py` — `ask()`: tries every configured provider/key in order until one answers; never raises, always returns `AskResult`. Builds tool schemas, tool executor, handles history/title updates.
- `ai_config.py` — provider config (multi-provider, multi-key), persona (assistant_name/address_user_as), defaults (tools_enabled, timeout).
- `ai_providers.py` — one adapter per provider: `call_openai_compatible`, `call_anthropic`, `call_gemini`, `call_cohere`, `call_ollama`. Shared signature `(provider, messages, timeout, tools=None, tool_executor=None)`. `ADAPTERS` dict maps type→function.
- `conversations.py` — multi-turn conversation storage (replaces old `history.py`). JSON files keyed by `secrets.token_hex(8)`. Key funcs: `get_current_id`, `set_current`, `new_conversation`, `get_conversation`, `list_conversations`, `delete_conversation`, `append_exchange`, `drop_from_user`, `clear`, `conversation_messages`, `is_valid_id`. Web UI passes `conversation_id` per browser tab via env var `JARVIS_CONVERSATION_ID`; plain CLI falls back to on-disk "current" pointer.
- `tools.py` — `TOOL_SCHEMAS`/`TOOLS` aggregate all tool modules. `execute_tool(name, arguments)` = single dispatch point, never raises. `tools_list_payload()` = full catalog incl. safety flags, powers debug dashboard. `compact_schemas_for_prompt()`/`name_only_schemas_for_prompt()` = schema-stripping for cheaper prompts. Merges: `command_tools`, `git_tools`, `memory`, `pkg_tools`, `playnite_api_tools`+`playnite_tools` (→ `PLAYNITE_TOOLS`), `radio_tools`, `screenshot_tools`, `spotify_tools`, `web_tools`, plus our own `file_tools` (write_file) and `custom_tools` (run_custom_command).
- `tool_safety.py` (ours) — per-tool `confirm_required`/`ai_review` flags, persisted at `~/.jarvis/tool_safety.json`. `DEFAULT_CONFIRM_REQUIRED` = all mutating tools. `DEFAULT_AI_REVIEW = {"run_custom_command"}`. API: `get_flags`, `requires_confirmation`, `requires_ai_review`, `set_flag`, `all_flags`.
- `file_tools.py` (ours) — `write_file(arguments)`: create/overwrite/append. Args `path`, `content`, `mode`. Relative paths resolve against home dir.
- `custom_tools.py` (ours) — `run_custom_command(arguments)`: arbitrary shell via `subprocess.run(shell=True)`. Most dangerous tool — defaults to both confirm_required and ai_review = True.
- `command_tools.py`, `commands_config.py`, `conditions.py` — user's saved custom commands (separate from ad-hoc AI tool calls); `commands.json` stores `run` templates with `{var}` placeholders.
- `git_tools.py`, `memory.py`, `pkg_tools.py`, `playnite_api_tools.py`, `playnite_tools.py`, `playnite_http.py`, `playnite_config.py`, `radio_tools.py`, `screenshot_tools.py`, `spotify_tools.py`, `spotify_api.py`, `spotify_config.py`, `web_tools.py`, `stats.py`, `palette.py` — per-domain tool modules, each exporting `<n>_TOOL_SCHEMAS`/`<n>_TOOLS`.
- `history.py` — legacy, superseded by `conversations.py`.

### Confirmation/risk-review architecture (our addition, in `ai_client.py`)
- `risk_review(tool_name, arguments, cfg, exclude_label=None)` — asks a *different* provider to assess danger. Best-effort, never raises, `None` on failure/no other provider.
- `_make_tool_executor(on_tool_call, schemas, on_confirm_request, cfg, provider_ref)` — closure given to adapters. Before a `confirm_required` tool runs: computes risk note if `ai_review`, calls `on_confirm_request` — tool only runs if it returns True. **No callback → fails closed.** `provider_ref` = one-element list mutated each provider-loop iteration.
- `ask()` signature: `ask(user_text, commands=None, on_attempt=None, on_tool_call=None, conversation_id=None, on_confirm_request=None)`.
- `cli.py`'s `on_confirm_request` closure: two protocols via `sys.stdin.isatty()`:
  - Real terminal: prints tool/args/risk, blocks on `input("Proceed? [y/N]: ")`.
  - Piped stdin (web server spawn): prints `JARVIS_CONFIRM_REQUEST {json}` to stdout, blocks on `sys.stdin.readline()`. `server.js` intercepts this exact prefix.
- New subcommands: `tool-preview <n> [json-args]` (computes flags/risk_note WITHOUT running — powers debug dashboard RUN button), `tool-safety-set <n> <confirm_required|ai_review> <true|false>`.

---

## web/ — Node server + frontend

### `server.js`
- Spawns `jarvis` binary for one-shot REST (`runJarvisOnce`) and streaming (`spawnAndStream`).
- **One active child per websocket** (`ws.activeChild`/`ws.activeKind`) — new run/ask blocked while one's in flight (`ask-error`: "Something's already running").
- `spawnAndStream`'s `onStdoutLine(line)`: return `true` to swallow a line instead of forwarding — used to intercept `JARVIS_CONFIRM_REQUEST {...}` and turn it into `ask-confirm-request`.
- WS client→server: `run`, `ask`, `cancel`, `ask-confirm-response` (`{approved}` writes `"y\n"`/`"n\n"` to stdin).
- WS server→client (ask family): `ask-start`, `ask-stdout`, `ask-stderr`, `ask-confirm-request` (`{tool, arguments, risk_note}`), `ask-exit`, `ask-error`.
- Ask text validation only rejects null bytes; newlines allowed (spawn uses argv arrays, never a shell).
- REST: `GET /api/tools`, `POST /api/tools/run`, `POST /api/tools/preview` (ours), `POST /api/tools/safety` (ours), `GET/POST /api/conversations`, `GET/DELETE /api/conversations/:id`, `GET /api/screenshots/:name`.
- `conversationEnv(conversationId)` builds `JARVIS_CONVERSATION_ID` env var.

### `web/public/index.html`
- Command-list panel (search under Config/+New), console panel, sequence builder.
- Ask overlay (`#ask-overlay`→`.ask-panel`): head (title/status/Clear/Debug), `.ask-body` grid with `.ask-thread` + `.ask-prompt__term` (`#ask-prompt-term` trace panel) + conversations sidebar (`#convo-list`).
- `#ask-input` is a `<textarea>` (multi-line; Enter sends, Shift+Enter newline, auto-grows).
- Debug dashboard (`#debug-overlay`): 3-pane — right = tool list (`#debug-tool-list`, from `/api/tools`), middle = arg form + Run + response (Organized/Raw toggle), left = `#debug-docs` (name, description, `.debug-docs__safety` toggles, params).

### `web/public/app.js` (~2600+ lines)
**Global `state`**: `activeConversationId`, `askConversationId`, `conversations[]`, `convoSearch`, `askTraceByConv` (per-conv trace persistence), `pendingConfirmByConv`, `askPendingBubble`, `askTraceBubble`, `askReplyLines`, `cmdSearch`, `debugTools`, `debugSelected`, `debugPendingConfirm`, `debugResponseMode`.

**Ask/reply pipeline:**
- `isViewingAskThread()` — `askConversationId == null || === activeConversationId`. Central guard.
- `insertIntoAskThread(msg)` — checks DOM containment before `insertBefore`, falls back to `appendChild` (stale-node safe).
- `addJarvisBubblePending`, `appendAskReplyLine`, `rerenderAskPendingBubble`, `finalizeAskBubble(overrideMessage)`.
- `splitConsoleDump(lines)` — separates model's signed reply (`"<n>: text"`, matched anywhere) from echoed console/tool output; pulls `[called ...]`/`[tool result` trace lines. Returns `{name, dump, reply}`.
- `ensureAskTraceBubble`/`renderAskTrace(dumpLines)` — "Console" bubble for dumped output (distinct from `#ask-prompt-term`).
- `addAskConfirmBubble(tool, args, riskNote, convId)` — Yes/No card; resolve sends `ask-confirm-response`, clears `pendingConfirmByConv[convId]`.

**Trace panel (per-conversation persistence):**
- `askPromptLine(text, cls)` — always records into `askTraceByConv[askConversationId]`; only touches DOM if `isViewingAskThread()`.
- `askPromptBegin`/`askPromptEnd(code, signal, errorMessage)` — DOM-mutating parts gated by visibility.
- `renderAskTraceForConv(convId)` — replays saved trace on switch.
- `setAskStatus`/`setAskPromptState` — gated by `isViewingAskThread()`.

**Conversation switching (`selectConversation(id)`):**
1. Leaving a still-running conversation → toast "Still replying in the other chat".
2. Fetch + `loadConversationIntoThread(record)` (rebuilds `#ask-thread` from scratch).
3. If that conversation's ask still running: regenerate `askPendingBubble` + rerender + status "thinking…".
4. `renderAskTraceForConv(id)`, `refreshAskBusyUI()`.
5. Replay buffered `pendingConfirmByConv[id]`.

**Busy/Stop UI:**
- `setRunning(running)` — sets `state.running`; non-ask console buttons unconditional; ask-panel buttons delegate to `refreshAskBusyUI()`.
- `refreshAskBusyUI()` — `busyHere = state.running && isViewingAskThread()`; scopes Stop button + input disable to the conversation actually running.

**Debug dashboard:**
- `Api.listTools/runTool/previewTool/setToolSafety` — REST wrappers.
- `debugToggleRow(tool, key, label)` — builds a toggle switch, POSTs `/api/tools/safety`, mutates in-memory immediately.
- `renderDebugDocs(tool)` — name, description, toggles, param docs.
- RUN flow: if `confirm_required`, calls `Api.previewTool` (no exec) first, stores `debugPendingConfirm`, shows Yes/No card — only calls `debugRunNow` after Yes.

**Other UI:** command-list search (`#cmd-search`, filters name+description live); hover quick-action buttons on command cards (▶ run, + add-to-sequence).

**Ask-form submit handler** (`#ask-form` submit): checks `ORGANIZE_JSON_RE` for `organize-json <text>` special command (bypasses `state.running`/ask path entirely — hits `/api/json/organize` REST, independent of the single-child constraint). Otherwise: blocks/toasts if `state.running` is true (there's only one active child per websocket, so an ask can't run concurrently with anything else on that connection — toast differs depending on whether the busy ask is this conversation's or another's), else starts a new conversation if needed, sends `{type: "ask", text, quote, conversationId}`.

---

## style.css notes
- Design tokens: `--bg-raised`, `--bg-panel-2`, `--border`, `--border-strong`, `--accent`, `--accent-soft`, `--accent-dim`, `--text`, `--text-dim`, `--text-dimmer`, `--green`, `--red`, `--gold`, `--font-mono`.
- **CSS specificity gotcha (hit twice):** overriding `.ask-msg--jarvis .ask-msg__bubble` (specificity 0,2,0) needs equal/higher specificity — a single-class override loses silently. Fix: two classes on the same element, e.g. `.ask-msg__bubble.ask-msg__bubble--console`.
- `.ask-msg--console`, `.ask-msg--confirm` — role-label color variants; `.ask-msg__bubble--console` (mono, dim, `--bg-raised`), `.ask-msg__bubble--confirm` (gold left border).
- `.safety-toggle`/`.safety-toggle__track`/`.safety-toggle__input` — debug dashboard's custom switch.
- `.debug-confirm`/`.debug-confirm__risk` — debug dashboard's Yes/No preview card.
- `.ask-input-row textarea` — `resize: none`, `max-height: 140px`, `overflow-y: auto`.
- `.debug-overlay` — `position: fixed; inset: 0; display: flex; align-items: stretch; justify-content: center;` (was `flex-end`, right-pinned bug, fixed this session).

---

## Known simplifications / not fully solved
- `risk_review` picks "first other eligible provider by priority order" — no way to pin a specific reviewer provider.
- `showAskScreenshot` (screenshot bubbles) not buffered per-conversation like trace/confirms are — a screenshot from a backgrounded ask won't reappear on switch-back.
- `write_file`/`run_custom_command` intentionally narrow (no delete, no binary writes) — scope limit, not a bug.
- Confirm/stdin protocol assumes exactly one child process per websocket — no true concurrent asks across conversations, by design.

## Fixes applied this project (chronological)
1. UI scrollable list + hover buttons
2. AI token-usage reduction
3. Variable-substitution `.format()` bug fix
4. Console-dump-vs-reply bubble splitting (2 iterations)
5. Command search field
6. Debug Dashboard (tools list/args/response/docs)
7. file-write + custom-command tools + confirmation/AI-review system (2 iterations, 2nd retargeted at refactored `conversations.py`)
8. Conversation-switching bug fixes + multi-line input support
9. **This session:** debug menu centering fix (`.debug-overlay` `flex-end`→`center`), ask-submit silent-block fix (added toast instead of silent `return` when `state.running` blocks a send from a different conversation)

## Recommended next steps if continuing
1. Ask which patches have actually been applied — repo re-uploaded ~14 times, drifts each time.
2. Always re-verify against the CURRENT uploaded zip: `py_compile` touched `.py`, `node --check` touched `.js`, `git apply --check` final patch against a pristine copy before handing over.
