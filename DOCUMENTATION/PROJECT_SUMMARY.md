# Jarvis + KDE Connect — Full Context Summary

Three repos: **jarvis-main** (Python CLI + Node/JS web console), **kdeconnect-kde-master** (Qt/C++ desktop daemon, this fork has a custom `jarvis` plugin), **kdeconnect-android-main** (Kotlin/Compose phone app, matching `jarvis` plugin). This fork already has several bespoke plugins beyond stock KDE Connect (`tailscale`, `callbridge`, `shizuku`, `jarvis`) that all follow the same pattern: a Qt C++ desktop plugin bridging to a local service, matched by a Kotlin/Compose Android plugin.

---

## 1. jarvis-main

### jarvis-cli/jarvis/ (Python package, `jarvis` command / `python -m jarvis`)
- **cli.py** — argv parsing, chained commands (`then`/`and`), AI "ask" handling.
  - `on_tool_call(name, arguments)`: prints a friendly `$ <label> <args>` trace line to stderr before every tool call.
  - `on_confirm_request(name, arguments, risk_note)`: two protocols — real TTY gets a blocking `input()` y/N prompt; piped stdin (spawned by web server) prints `JARVIS_CONFIRM_REQUEST {json}` to **stdout** and blocks on `sys.stdin.readline()` for `"y\n"`/`"n\n"`.
  - Subcommands: `tools-list`, `tool-run <name> <argsJson>`, `tool-preview`, `conv-new/list/show/switch/delete`, various `*-config` path printers (`ai-config`, `everything-config`, `spotify-config`, `playnite-config`, `memory-config`).
- **everything_tools.py** — Windows-only voidtools **Everything** SDK via `ctypes`. Loads `Everything64.dll`/`Everything32.dll` from config override or candidate install dirs.
  - `search_files(arguments)`: Everything search syntax (`ext:`, `path:`, `size:`, quotes); params `query, max_results, match_case, match_whole_word, match_path, regex, sort`. Returns `{ok, query, count, total_matches, truncated, results:[{path,name,is_folder,size_bytes?,date_modified?}]}`.
  - `reveal_in_explorer({path})`: Windows-only, `subprocess.Popen(["explorer", f"/select,{path}"])`.
  - `open_file_location({path})`: opens containing folder (or itself if already a dir) via `os.startfile`.
  - `open_file({path})` — **added by me (turn A)**: opens the file itself via `os.startfile`; explicitly refuses folders with an error pointing to the other two tools; Windows-only.
  - All three registered in `EVERYTHING_TOOL_SCHEMAS` + `EVERYTHING_TOOLS`. Shared `_resolve_target_path()` helper (expands `~`, checks existence).
- **everything_config.py** — settings at `~/.jarvis/everything.json` (`dll_path`, `max_results_cap`, `default_max_results`, `match_path_default`, enable flag).
- **json_tools.py** — `organize_json` tool: validates/pretty-prints JSON as a tree. Uses the **JARVIS_MEDIA side-channel** (see §3) so the AI model only ever sees `{ok,type,top_level_count}` — the full JSON reaches the web UI directly via a stderr line, never spending model tokens.
- **screenshot_tools.py** — screenshot tool, emits `JARVIS_MEDIA\tscreenshot\t<filename>`.
- **ytdl_tools.py** — yt-dlp download tool, emits `JARVIS_MEDIA\tytdl_download\t<job_id>\t<filename>\t<label>`.
- **tool_safety.py** — flags which tools are `confirm_required`.
- **ai_client.py** — multi-provider tool-calling client (OpenAI, Anthropic, Gemini, xAI, Mistral, Groq, DeepSeek, OpenRouter, Ollama). `ask(text, commands, on_attempt, on_tool_call, conversation_id, on_confirm_request)`.
- **conversations.py** — on-disk conversation history, `get_current_id()`/`is_valid_id()`.
- **tools.py** — has a comment noting some tools are "used by the KDE Connect plugin via `JARVIS_ALLOWED_TOOLS`".
- DOCUMENTATION/: `jarvis_everything_i_know.md`, `everything_sdk_reference.md`, `everything_sdk_python_reference.md`.

### web/ (Node/Express + vanilla JS)
- **server.js** — spawns `jarvis` (tries `jarvis`, `python3 -m jarvis`, `python -m jarvis`, `py -m jarvis`).
  - `GET /api/status`, `GET /api/tools` (→ `tools-list`).
  - `POST /api/tools/run {name,arguments}` → `jarvis tool-run` — runs one tool directly (bypasses the AI). **Always 200**; success/failure lives inside `result.error`, not HTTP status. This is what the debug dashboard buttons *and* the new KDE Connect file-action buttons call.
  - `POST /api/tools/preview` → `jarvis tool-preview` — checks if a tool would need confirmation + AI risk note, without running it.
  - WebSocket: `{"type":"run"}` / `{"type":"ask"}` spawn `jarvis` and stream stdout/stderr as `stdout/stderr/exit` or `ask-stdout/ask-stderr/ask-exit/ask-error`. Watches ask-stdout for a `JARVIS_CONFIRM_REQUEST {...}` line and converts it to `{"type":"ask-confirm-request", tool, arguments, risk_note}` instead of forwarding it as text; writes `"y\n"/"n\n"` back to the child's stdin on `{"type":"ask-confirm-response"}`.
  - Also: `/api/commands` CRUD, `/api/config/list` + `/api/config/file/<name>/raw` (generic config browser), `/api/conversations`, `/api/json/organize`, `/api/screenshots/<file>`, `/api/downloads/<jobId>/<filename>`.
- **public/app.js** — browser console UI.
  - `addAskPromptTrace(raw)`: single dispatcher for every ask-stderr line; intercepts `JARVIS_MEDIA\t<kind>\t...` for kind `screenshot` (→ `showAskScreenshot`, fetches `/api/screenshots/..`), `download` (→ `showAskDownload`, inline audio/video player), `organize_json` (→ `showAskOrganizeJson`, re-fetches via REST for the full tree at zero extra AI tokens). Everything else becomes a plain `$ ...` trace line.
  - `addAskConfirmBubble(tool,args,riskNote,convId)`: the **buttony Yes/No confirmation card** — `btn--primary "Yes, run it"` / `btn--ghost "No, cancel"`; sends `{"type":"ask-confirm-response",approved}`.
  - Debug Dashboard: run any tool directly via preview→run. `debugFileActionButtons(path,isFolder)` renders "Reveal in Explorer" / "Open location" / **"Open file"** (added turn A, hidden when `isFolder`) — each just POSTs `/api/tools/run`.
- **public/style.css** — dark cyan/gold theme. `.ask-msg--confirm`/`.ask-confirm__*` (gold Yes/No card), `.ask-msg--console` (raw trace bubble), `.ask-shot-*`/`.ask-dl-*` (media bubbles), `.debug-file-row`/`.debug-file-actions`.

### Changes made to jarvis-main this conversation
1. **Turn A** (`jarvis-main_21` → `jarvis-main_22`, **kept**): added `open_file` tool to `everything_tools.py` + its "Open file" debug button in `app.js`. Delivered as `jarvis-main_22.zip` and as `open_file_debug_button.patch`.
2. **Turn C** (attempted a chat-facing "buttony" feature *inside* jarvis-main — **fully reverted**): had added `_emit_file_actions()` (a `JARVIS_MEDIA\tfile_actions\t{...}` emission from `search_files`), `showAskFileActions()` in `app.js`, and new CSS. User said *"add this in the kde mobile plugin not the main html thing"* → all three files (`everything_tools.py`, `app.js`, `style.css`) reverted byte-for-byte back to `jarvis-main_22.zip` (confirmed via `diff`/`cmp`).
3. **Current state: jarvis-main is unmodified beyond Turn A.** No JARVIS_MEDIA `file_actions` signal exists there. `/api/tools/run` was reused as-is (not touched) by the KDE Connect work below.

---

## 2. The custom "Jarvis" KDE Connect plugin (pre-existing scaffolding, extended by me)

Lets a paired phone remote-control the jarvis-web server on the desktop. Both directions share two packet types with a `type`/`action` discriminator field: `kdeconnect.jarvis` (desktop→phone) and `kdeconnect.jarvis.request` (phone→desktop). Declared in `kdeconnect_jarvis.json`, `EnabledByDefault: false`.

### Desktop — `kdeconnect-kde-master/plugins/jarvis/` (jarvisplugin.h/.cpp, kdeconnect_jarvis.json, kdeconnect_jarvis_config.qml, CMakeLists.txt)
- Bridges to jarvis-web at `http://127.0.0.1:4173` (override `JARVIS_WEB_URL`); can auto-start it via `tryStartNode()` if `JARVIS_WEB`/`~/.jarvis/web` is found.
- `receivePacket(np)` dispatches `np["action"]`: `listCommands/requestStatus, createCommand, updateCommand, deleteCommand, getConfigList, getConfig, setConfig, run, ask, cancel, aiClear, askConfirmResponse`, and now **`fileAction`**.
- `onWsTextMessage(message)` translates jarvis-web's WS events into phone packets: `commands→sendCommands`, `start/stdout/stderr/exit→runStart/runStdout/runStderr/runExit`, `ask-start→askStart`, `ask-stdout→askStdout`, `ask-stderr→askStderr` (also intercepts `JARVIS_MEDIA\tscreenshot\t<file>` to fetch+forward the image as a `screenshot` packet), `ask-exit/ask-error→askExit`, `ask-confirm-request→askConfirmRequest`.
- `ensureConversationId()`: the plugin keeps its **own** persisted conversation id (separate from any browser tab).
- `allowedToolsEnv()`: per-tool phone permissions → `JARVIS_ALLOWED_TOOLS` env var.

**New for the file-actions feature (Turn D/E):**
- Includes added: `<QFileInfo>`, `<QHash>`, `<QPair>`, `<QVector>`.
- `extractCandidatePaths(text)` (anon namespace): regex `\b[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*[^\\/:*?"<>|\r\n]+` finds bare Windows absolute paths; trims trailing/leading punctuation/quotes.
- New member `QVector<QPair<QString,bool>> m_askFilePaths` (path, isFolder) for the *current* ask turn.
- `collectFileActionCandidates(line)`: called on every `ask-stdout` line; extracts candidates, dedupes case-insensitively, verifies each with a **local `QFileInfo::exists()`** check (desktop runs on the same PC — no round trip needed), tags `isDir()`; capped at 20/turn.
- `sendCollectedFileActions()`: called at `ask-exit`/`ask-error`; if non-empty, sends new packet type **`askFileActions`** `{id, pathsJson: [{path,isFolder}]}`; clears the buffer.
- `m_askFilePaths.clear()` also added at the start of `handleAsk()`.
- `handleFileAction(np)` (NEW): reads `path` + `fileAction` (`reveal`/`openLocation`/`openFile`), maps via `QHash` to the jarvis tool name (`reveal_in_explorer`/`open_file_location`/`open_file`), **POSTs to jarvis-web's existing `/api/tools/run`** (no jarvis-main changes needed), checks `result.error` in the JSON body (since the endpoint always returns HTTP 200), replies `ok {action:"fileAction",fileAction,path}` or `error {message}`.
- `receivePacket()` gained: `if (action == "fileAction") { handleFileAction(np); return; }`.

### Phone — `kdeconnect-android-main/src/main/java/org/kde/kdeconnect/plugins/jarvis/` (JarvisPlugin.kt, JarvisActivity.kt, JarvisScreens.kt, JarvisMarkdown.kt)
- **JarvisPlugin.kt**: Compose-observable state (`online, statusError, commandsJson, lastError, runOutput, askMessages, askConsole, configTexts/configPaths/configFiles, busy, sequence, jobId`).
  - `onPacketReceived(np)` mirrors desktop's outgoing types 1:1, including `askConfirmRequest` (inserts an `isConfirm=true` bubble) and `screenshot` (inline image bubble).
  - **NEW `askFileActions` case**: parses `pathsJson` via `parseFileActionEntries()`; if non-empty, inserts `JarvisChatMessage(isFileActions=true, fileActionsJson=pathsJson)` at the same insertion point as the console bubble (right before the still-streaming assistant bubble).
  - **NEW `fun fileAction(path, kind)`**: `sendAction("fileAction"){ it["path"]=path; it["fileAction"]=kind }`.
  - `splitConsoleDump(lines)` / `NAME_PREFIX_LINE` / `INLINE_TOOL_TRACE_LINE`: Kotlin port of `app.js`'s identical logic (kept in sync) to separate the model's real reply from echoed tool-call/tool-result trace text.
  - `JarvisChatMessage` data class gained **`isFileActions: Boolean`, `fileActionsJson: String?`**.
  - **NEW**: `data class JarvisFileActionEntry(path, isFolder)` + `fun parseFileActionEntries(json): List<JarvisFileActionEntry>` (tolerant parse, empty list on malformed input).
  - `PACKET_TYPE = "kdeconnect.jarvis"`, `PACKET_TYPE_REQUEST = "kdeconnect.jarvis.request"`.
- **JarvisScreens.kt** (Jetpack Compose):
  - `AskScreen`: `LazyColumn` over `askMessages`, branching `isConfirm→AskConfirmBubble`, **NEW `isFileActions→AskFileActionsBubble`**, `isConsole→AskConsoleBubble`, else normal chat bubble (Markdown via `JarvisMarkdownText`, inline screenshots, "thinking…" placeholder).
  - `AskConfirmBubble` (reference pattern mirrored): `errorContainer`-colored Card, tool name + pretty JSON args + optional risk note, resolved ✓/✗ label or `OutlinedButton("No, cancel")` + `Button("Yes, run it")`.
  - **NEW `AskFileActionsBubble(msg, onAction)`**: `secondaryContainer`-colored Card titled "Found on your PC"; per `JarvisFileActionEntry`, monospace path + button row: "Reveal in Explorer", "Open location", and "Open file" (**only when `!isFolder`**); wired to `plugin.fileAction(path, kind)`.
- **strings.xml** — NEW: `jarvis_file_actions_title` = "Found on your PC", `jarvis_file_reveal` = "Reveal in Explorer", `jarvis_file_open_location` = "Open location", `jarvis_file_open` = "Open file".
- Pre-existing, **not modified**, patch files already shipped in this repo (prior art for the same "buttony" pattern): `kdeconnect-android-jarvis-confirm-bubble.patch`, `my_changes.patch`, `jarvis-android.patch`.

### End-to-end flow
1. Phone sends `ask` → desktop relays to jarvis-web → jarvis-cli runs the AI turn (may call `search_files` etc.) and mentions paths in its reply.
2. Desktop scans every `askStdout` line live, keeps only paths that exist locally (`QFileInfo`), tags folder-vs-file.
3. At `ask-exit`, desktop sends one `askFileActions` packet.
4. Phone shows a "Found on your PC" bubble with Reveal/Open location/(Open file if not a folder) buttons.
5. Tap → phone sends `fileAction {path, kind}` back.
6. Desktop maps `kind` → jarvis tool name, calls the **same** `/api/tools/run` the browser debug dashboard uses — so the actual OS logic lives in exactly one place (`everything_tools.py`).
7. Result → `ok`/`error` packet; errors surface via the phone's existing `lastError`.

---

## 3. The JARVIS_MEDIA pattern (core architectural idiom, used throughout)
A tool function prints `JARVIS_MEDIA\t<kind>\t<payload>` to **stderr** (never to the model). The web UI's `addAskPromptTrace` and the desktop KDE plugin's `onWsTextMessage` (`ask-stderr` case) both watch for this prefix and route it to rich renderers, while the AI model itself only ever receives a tiny summary dict as the tool's actual return value — keeping large/binary/sensitive payloads (screenshots, JSON blobs, download links) out of the token stream entirely. Used by `screenshot_tools.py`, `json_tools.py`, `ytdl_tools.py`. **Not** used for the new file-actions feature (that lives entirely in the KDE Connect plugins, sourced from reply text + local filesystem checks, per explicit instruction not to touch jarvis-main further).

---

## 4. Deliverables (chronological)
1. `jarvis-main_22.zip` — full repo with `open_file` tool + debug button.
2. `open_file_debug_button.patch` — diff of just that (applies with `patch -p1` from jarvis-main root).
3. `kde_jarvis_file_actions.patch` — diff of `jarvisplugin.h` + `jarvisplugin.cpp` (applies from `kdeconnect-kde-master` root).
4. `android_jarvis_file_actions.patch` — diff of `JarvisPlugin.kt` + `JarvisScreens.kt` + `strings.xml` (applies from `kdeconnect-android-main` root).
5. jarvis-main itself: **unchanged** beyond #1 (the Turn C chat-bubble attempt was fully reverted).

## 5. Open items / not done
- No Linux-native Explorer equivalent — the three tools stay Windows-only at the `everything_tools.py` level, so phone buttons will surface a "...is Windows-only" error if jarvis-cli isn't running on Windows.
- Path regex only catches bare `C:\...` paths — no UNC (`\\server\share`) or quoted-path-with-embedded-spaces handling beyond simple trimming.
- No actual Qt/Gradle build was run (no toolchain/network in this environment) — changes were reviewed by hand plus brace/paren balance checks only.
- Full re-zipped `kdeconnect-kde-master_7.zip` / `kdeconnect-android-main_6.zip` were generated locally but **not delivered** — only the two patches were, per explicit request to stop re-verifying and just send patches.
