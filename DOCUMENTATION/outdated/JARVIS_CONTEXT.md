# Jarvis — Full Context Handoff

Personal AI-assistant: CLI + web console. Repo root = `jarvis-main/`, two halves:
- `jarvis-cli/jarvis/` — Python backend/CLI (a fresh process per invocation, no persistent session)
- `web/` — Node/Express server + vanilla JS/HTML/CSS frontend

**Workflow used throughout this project's history:** unzip the uploaded repo, edit a working copy, generate a `git diff --no-index`-style `.patch` (paths rewritten to match real repo layout: `a/path b/path`), verify `git apply --check` against a pristine copy, hand the user a downloadable `.patch` to run `git apply file.patch` from repo root and push themselves. **Multiple patches have been produced across sessions — check with the user which have actually been applied before touching these files, and re-fetch current repo state.** The codebase has been re-uploaded several times (jarvis-main_2, _3, _5, _9, _11...) as the user applies patches and makes their own changes; do not assume the base state, always inspect the actual uploaded zip.

---

## jarvis-cli/jarvis/ — Python backend modules

- `cli.py` — entry point, argv dispatch. `handle_ai_prompt(text, commands)` is the ask handler. Reserved subcommand names live in `RESERVED_NAMES`. Key subcommands: `config`, `ai-config`, `ai-clear`, `ai-drop-from`, `tools-list`, `tool-run`, `tool-preview`, `tool-safety-set`, `conv-new/list/show/switch/delete`.
- `ai_client.py` — the core `ask()` function: tries every configured/enabled provider in order, then every key within a provider, until one answers. Never raises — always returns an `AskResult`. Builds tool schemas, constructs the tool executor, handles conversation history/title updates.
- `ai_config.py` — provider config (multiple providers, multiple keys each), persona (assistant_name/address_user_as), defaults (tools_enabled, timeout).
- `ai_providers.py` — one adapter function per provider type: `call_openai_compatible`, `call_anthropic`, `call_gemini`, `call_cohere`, `call_ollama`. All share signature `(provider, messages, timeout, tools=None, tool_executor=None)`. `ADAPTERS` dict maps type→function.
- `conversations.py` — multi-turn conversation/history storage (replaced the older `history.py` module). Conversations are JSON files keyed by `secrets.token_hex(8)` ids. Key functions: `get_current_id`, `set_current`, `new_conversation`, `get_conversation`, `list_conversations`, `delete_conversation`, `append_exchange`, `drop_from_user`, `clear`, `conversation_messages`, `is_valid_id`. The web UI passes an explicit `conversation_id` per browser tab via env var `JARVIS_CONVERSATION_ID`; plain CLI use falls back to the on-disk "current" pointer.
- `tools.py` — aggregates all tool schemas/dispatch. `TOOL_SCHEMAS` = concatenation of every module's schemas. `TOOLS` = merged dict name→function. `execute_tool(name, arguments)` — the single dispatch point, never raises. `tools_list_payload()` — full catalog incl. parameters AND (see tool_safety below) each tool's live `confirm_required`/`ai_review` flags; powers the web debug dashboard. `compact_schemas_for_prompt()` / `name_only_schemas_for_prompt()` — schema-stripping for sending to the model efficiently. Imports and merges: `command_tools`, `git_tools`, `memory` (MEMORY_TOOL*), `pkg_tools`, `playnite_api_tools` + `playnite_tools` (merged into one `PLAYNITE_TOOLS`/`PLAYNITE_TOOL_SCHEMAS`), `radio_tools`, `screenshot_tools`, `spotify_tools`, `web_tools`, and the two tools added in this project: `file_tools` (write_file) and `custom_tools` (run_custom_command).
- `tool_safety.py` — **added by us.** Per-tool safety toggles, persisted at `~/.jarvis/tool_safety.json`. Two independent flags per tool: `confirm_required` (pause and ask Y/N before running) and `ai_review` (get a second AI provider's danger assessment first). `DEFAULT_CONFIRM_REQUIRED` set includes all mutating tools (write_file, run_command, run_chain, run_custom_command, create/update/delete_command, package_install/uninstall, memory_forget, wifi_set, bluetooth_set, git_run). `DEFAULT_AI_REVIEW = {"run_custom_command"}`. API: `get_flags(name)`, `requires_confirmation`, `requires_ai_review`, `set_flag(name, key, value)`, `all_flags(names)`.
- `file_tools.py` — **added by us.** `write_file(arguments)` tool: create/overwrite/append a text file. Args: `path`, `content`, `mode` (overwrite/append/create_only). Relative paths resolve against home dir.
- `custom_tools.py` — **added by us.** `run_custom_command(arguments)` tool: arbitrary shell command via `subprocess.run(shell=True, ...)`. Args: `command`, optional `timeout` (default 20s). This is the most dangerous tool — defaults to BOTH confirm_required and ai_review = True.
- `command_tools.py`, `commands_config.py`, `conditions.py` — the user's saved custom commands system (separate from ad-hoc AI tool calls). `commands.json` stores named commands with `run` templates using `{var}` placeholders.
- `git_tools.py`, `memory.py`, `pkg_tools.py`, `playnite_api_tools.py`, `playnite_tools.py`, `playnite_http.py`, `playnite_config.py`, `radio_tools.py`, `screenshot_tools.py`, `spotify_tools.py`, `spotify_api.py`, `spotify_config.py`, `web_tools.py`, `stats.py`, `palette.py` — individual tool-domain modules, each exporting `<NAME>_TOOL_SCHEMAS` + `<NAME>_TOOLS` dict, following the same pattern.
- `history.py` — legacy history module (largely superseded by `conversations.py` in later versions of the repo).

### Confirmation/risk-review architecture (our addition, in `ai_client.py`)

- `risk_review(tool_name, arguments, cfg, exclude_label=None)` — asks a *different* configured provider than the one currently answering to explain what a tool call does and how dangerous it is. Best-effort, never raises, returns `None` on any failure or if no other provider configured. Returns `{"provider": label, "note": text}`.
- `_make_tool_executor(on_tool_call, schemas=None, on_confirm_request=None, cfg=None, provider_ref=None)` — the closure passed to provider adapters as `tool_executor`. Before running any tool where `tool_safety.requires_confirmation(name)` is true: computes a risk note if `requires_ai_review`, then calls `on_confirm_request(name, arguments, risk_note)` — tool only runs if it returns True. **No callback supplied → fails closed** (returns a cancelled result, never silently runs). `provider_ref` is a one-element list `[current_provider_label]` mutated by `ask()` on each provider-loop iteration, so `risk_review` knows which provider to exclude.
- `ask()` signature: `ask(user_text, commands=None, on_attempt=None, on_tool_call=None, conversation_id=None, on_confirm_request=None)`.
- `cli.py`'s `on_confirm_request(name, arguments, risk_note=None)` closure inside `handle_ai_prompt`: **two protocols, auto-selected by `sys.stdin.isatty()`:**
  - Real terminal: prints the tool/args/risk note, blocks on `input("Proceed? [y/N]: ")`.
  - Piped stdin (spawned by web server): prints `JARVIS_CONFIRM_REQUEST {json}` to **stdout**, then blocks on `sys.stdin.readline()` for the answer. `server.js` intercepts this exact line prefix.
- New CLI subcommands: `tool-preview <name> [json-args]` (computes confirm_required/ai_review/risk_note WITHOUT running the tool — used by the debug dashboard's RUN button before actually executing), `tool-safety-set <name> <confirm_required|ai_review> <true|false>`.

---

## web/ — Node server + frontend

### `server.js`
- Spawns the `jarvis` binary as a child process for both one-shot REST calls (`runJarvisOnce(args, timeoutMs, extraEnv)`) and long-lived streaming (`spawnAndStream(ws, kind, fullArgs, types, extraEnv, onStdoutLine)`).
- **One active child per websocket connection** (`ws.activeChild`/`ws.activeKind`) — a "run" or "ask" already in flight blocks a new one from starting (`ask-error`: "Something's already running").
- `spawnAndStream`'s `onStdoutLine(line)` param: return `true` to swallow a stdout line instead of forwarding it as a normal message — used to intercept `JARVIS_CONFIRM_REQUEST {...}` lines and turn them into a proper `ask-confirm-request` websocket message instead of dumping raw JSON into the chat.
- Websocket message types (client→server): `run`, `ask`, `cancel`, `ask-confirm-response` (`{approved: bool}` — writes `"y\n"`/`"n\n"` to `ws.activeChild.stdin`, unblocking the CLI's blocked `readline()`).
- Websocket message types (server→client, "ask" family): `ask-start`, `ask-stdout`, `ask-stderr`, `ask-confirm-request` (`{tool, arguments, risk_note}`), `ask-exit`, `ask-error`.
- **Ask text validation:** only rejects null bytes (`\0`) now — newlines are explicitly ALLOWED (we removed an overly-cautious `\r\n` rejection; `spawn()` uses argv arrays, never a shell, so newlines were never actually an injection risk).
- REST endpoints of note: `GET /api/tools` (tools-list, incl. safety flags), `POST /api/tools/run`, `POST /api/tools/preview` (our addition — checks flags + computes risk note, no execution), `POST /api/tools/safety` (our addition — flips one flag), `GET/POST /api/conversations`, `GET/DELETE /api/conversations/:id`, `GET /api/screenshots/:name`.
- `conversationEnv(conversationId)` — builds the `JARVIS_CONVERSATION_ID` env var passed to spawned processes.

### `web/public/index.html`
- Main layout: command-list panel (with search field under Config/+New), console panel, sequence builder.
- Ask overlay (`#ask-overlay` → `.ask-panel`): `.ask-panel__head` (title/status/actions incl. Clear/Debug buttons), `.ask-body` grid with `.ask-thread` (chat bubbles) + `.ask-prompt__term` (`#ask-prompt-term`, the live "commands Jarvis runs" trace panel) + conversations sidebar (`#convo-list`, search).
- `#ask-input` is a **`<textarea>`** (converted from `<input type="text">` in our last patch) — multi-line support, Enter sends / Shift+Enter newline, auto-grows via JS.
- Debug dashboard (`#debug-overlay`): three-pane tool inspector — right = tool list (`#debug-tool-list`, searchable, reads from `/api/tools`, NOT hardcoded), middle = argument form + Run button + response pane (Organized/Raw JSON toggle via `#debug-response-toggle`), left = `#debug-docs` (name, description, **`.debug-docs__safety`** with "Toggle warning:"/"Toggle AI review:" switches right under the description, then Parameters docs).

### `web/public/app.js` — key functions/state (roughly 2600+ lines)

**Global `state` object** includes (non-exhaustive, relevant to recent work):
- `activeConversationId`, `askConversationId` (which conversation an in-flight ask actually belongs to — can differ from `activeConversationId` if the user switched away), `conversations[]`, `convoSearch`
- `askTraceByConv` — `{convId: [{text, cls}]}` — **persists the trace panel per conversation** so switching away and back doesn't lose it
- `pendingConfirmByConv` — `{convId: {tool, arguments, risk_note}}` — buffers a confirm request that arrived while its conversation wasn't the one on screen
- `askPendingBubble`, `askTraceBubble`, `askReplyLines`, `cmdSearch`, `debugTools`, `debugSelected`, `debugPendingConfirm`, `debugResponseMode`

**Ask/reply pipeline:**
- `isViewingAskThread()` — `state.askConversationId == null || === state.activeConversationId`. Central guard used everywhere that would otherwise paint into the wrong conversation's DOM.
- `insertIntoAskThread(msg)` — **safe insert helper**: checks `askThread.contains(state.askPendingBubble)` before using it as an `insertBefore` reference (a stale/detached reference throws outright) — falls back to `appendChild`.
- `addJarvisBubblePending()`, `appendAskReplyLine(line)`, `rerenderAskPendingBubble()` (shared re-render logic, also used to repaint a freshly-recreated pending bubble after a conversation switch), `finalizeAskBubble(overrideMessage)`.
- `splitConsoleDump(lines)` — separates a model's own signed reply (`"<Name>: text"` prefix, matched anywhere in the line list, not just line 1) from console/tool-output the model echoed verbatim ahead of it. Also pulls out inline `[called ...]`/`[tool result` trace lines. Returns `{name, dump, reply}`.
- `ensureAskTraceBubble()` / `renderAskTrace(dumpLines)` — the "Console" bubble for dumped tool output within a reply (distinct from the `#ask-prompt-term` trace panel below). Also stale-node-guarded.
- `addAskConfirmBubble(tool, args, riskNote, convId)` — renders the Yes/No confirmation card in the chat thread; `resolve(approved)` sends `ask-confirm-response`, clears `state.pendingConfirmByConv[convId]`.

**Trace panel (`#ask-prompt-term`) — per-conversation persistence (fixed in last session):**
- `askPromptLine(text, cls)` — the single low-level line-adder. **Always records** into `state.askTraceByConv[state.askConversationId]` regardless of visibility; only touches the DOM if `isViewingAskThread()`.
- `askPromptBegin()` / `askPromptEnd(code, signal, errorMessage)` — reset/finalize; DOM-mutating parts gated by visibility (previously were NOT gated — this caused stray "done"/"exit N" lines to leak into whatever unrelated conversation happened to be on screen).
- `renderAskTraceForConv(convId)` — replays a conversation's saved trace log on switch (instead of the old unconditional `askPromptReset()`), restoring the live cursor if that conversation's ask is still running.
- `setAskStatus(text, kind)` / `setAskPromptState(text, live)` — both now gated by `isViewingAskThread()`.

**Conversation switching (`selectConversation(id)`):**
1. If leaving a conversation whose ask is still running, toast "Still replying in the other chat".
2. Fetch + `loadConversationIntoThread(record)` (rebuilds `#ask-thread` from scratch — this is why stale DOM references were a real crash risk).
3. **If that conversation's ask is still running:** regenerate `state.askPendingBubble` fresh + `rerenderAskPendingBubble()` + `setAskStatus("thinking…")` — without this, subsequent live updates threw on the destroyed old bubble node (this was the actual root cause of tool calls appearing to "stop" when switching away and back).
4. `renderAskTraceForConv(id)`, `refreshAskBusyUI()`.
5. Replay any buffered `pendingConfirmByConv[id]`.

**Busy/Stop-button UI:**
- `setRunning(running)` — sets `state.running`, handles the *non-ask* console run buttons unconditionally, delegates ask-panel buttons to `refreshAskBusyUI()`.
- `refreshAskBusyUI()` — `busyHere = state.running && isViewingAskThread()`; only this scopes the Stop button/disabled-input state — fixes the bug where Stop stayed visible after switching to an unrelated, idle conversation.

**Debug dashboard:**
- `Api.listTools/runTool/previewTool/setToolSafety` — thin wrappers over the REST endpoints.
- `debugToggleRow(tool, key, label)` — builds one "Toggle warning:"/"Toggle AI review:" switch, POSTs to `/api/tools/safety` on change, mutates the in-memory tool object for immediate feedback.
- `renderDebugDocs(tool)` — name, description, the two toggles, then parameter docs.
- RUN button flow: if `tool.confirm_required`, calls `Api.previewTool` first (no execution), stores `state.debugPendingConfirm`, renders `debugRenderConfirmPending(pending)` (Yes/No card, shown in both Organized and Raw JSON modes) — only calls `debugRunNow(name, args)` (the real `/api/tools/run`) after Yes.

**Other UI additions from earlier in the project:**
- Command-list search field (`#cmd-search`) right under Config/+New, filters by name+description live.
- Hover quick-action buttons on command cards (▶ run, + add-to-sequence).

---

## style.css notes
- Design tokens: `--bg-raised`, `--bg-panel-2`, `--border`, `--border-strong`, `--accent`, `--accent-soft`, `--accent-dim`, `--text`, `--text-dim`, `--text-dimmer`, `--green`, `--red`, `--gold`, `--font-mono`.
- **CSS specificity gotcha hit twice:** overriding `.ask-msg--jarvis .ask-msg__bubble` (2-class ancestor+descendant selector, specificity 0,2,0) requires a selector of EQUAL OR HIGHER specificity — a single-class override like `.ask-msg__bubble--console` (0,1,0) silently loses. Fix: use `.ask-msg__bubble.ask-msg__bubble--console` (two classes on the same element, 0,2,0) — applies to both the console-dump bubble and the confirm bubble.
- `.ask-msg--console`, `.ask-msg--confirm` — role-label color variants; `.ask-msg__bubble--console` (monospace, dim, `--bg-raised`), `.ask-msg__bubble--confirm` (gold left border).
- `.safety-toggle` / `.safety-toggle__track` / `.safety-toggle__input` — the debug dashboard's switch UI (custom checkbox, not native).
- `.debug-confirm` / `.debug-confirm__risk` — the debug dashboard's Yes/No preview card.
- `.ask-input-row textarea` — `resize: none`, `max-height: 140px`, `overflow-y: auto`.

---

## Known simplifications / things flagged but not fully solved
- `risk_review` picks "the first other eligible provider by priority order" — doesn't target a specific named provider (e.g. always-prefer-Claude-for-review). Would need a small config addition if the user wants that.
- `showAskScreenshot` (screenshot media bubbles) still isn't buffered per-conversation the way trace lines/confirms now are — a screenshot taken by a backgrounded ask while viewing another conversation will silently not appear when switching back. Minor, not yet fixed.
- File-write/custom-command tools are narrow by design (no delete, no binary writes for `write_file`) — intentional scope limit, not a bug.
- The whole confirm/stdin protocol assumes exactly one child process per websocket connection; it was never designed for true concurrent asks across conversations (matches the app's existing single-active-child architecture, not something we changed).

## Recommended next steps if continuing
1. Ask the user directly which of the patches below they've actually applied — the repo has been re-uploaded ~5 times across this project's history and drifted each time; don't assume.
2. Patches produced so far (chronological): UI scrollable list + hover buttons; AI token-usage reduction; variable-substitution `.format()` bug fix; console-dump-vs-reply bubble splitting (2 iterations); command search field; Debug Dashboard (tools list/args/response/docs, built by a prior session before context here); file-write + custom-command tools + confirmation/AI-review system (2 iterations, second one re-targeted at a refactored codebase with a new `conversations.py`); conversation-switching bug fixes + multi-line input support (this session).
3. Always re-verify against the CURRENT uploaded zip: `py_compile` all touched `.py`, `node --check` all touched `.js`, and `git apply --check` the final patch against a pristine copy before handing it over — patches silently drift out of compatibility as the user's own edits accumulate.
