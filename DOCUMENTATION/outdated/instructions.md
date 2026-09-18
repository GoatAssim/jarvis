# JARVIS — organize-json + dynamic Settings — handoff

Patch: `jarvis-json-organize-and-settings.patch` (apply from repo root: `git apply jarvis-json-organize-and-settings.patch`)

Verified before hand-off:
- `git apply --check` — clean, zero warnings, against a pristine copy of the uploaded zip
- Applying the patch produces a tree byte-identical to my working copy
- `node --check web/public/app.js` — passes
- `python3 -m py_compile jarvis-cli/jarvis/json_tools.py jarvis-cli/jarvis/cli.py` — passes
- Ran `jarvis organize-json` by hand against both a valid and a deliberately broken JSON file — tree output, `--raw`, `--json`, and the invalid-JSON error+line/column+caret snippet all look right

---

## Things done (this patch)

**Backend (already in your uploaded zip, I only added the missing piece):**
- `jarvis-cli/jarvis/json_tools.py` — **new file, was missing** (referenced by `cli.py` but didn't exist, so `organize-json` was completely broken before this patch). Pure local file read + `json` stdlib parsing, no `ai_client`/`ai_providers` import anywhere in the call path — so pointing it at a huge file costs zero API tokens no matter what. Normalized to LF line endings to match the rest of the repo (the copy you sent me was CRLF).
  - `read_json_file(path)` → `(data, text, error)`. Invalid JSON returns a clear message with exact line/column plus a `^`-caret snippet.
  - `render_tree(data)` → human-readable branch/indent outline (├─/└─ characters, type + size annotations like `{} (3 keys)` / `[] (2 items)`) — deliberately not `json.dumps`, so it reads like a file listing, not source code.
- `cli.py`, `server.js`, `RESERVED_NAMES` — already wired to `json_tools` in your upload; I didn't need to touch these. `organize-json <path> [--raw] [--json]` was already in the dispatch table.
- `server.js` REST surface — already generic in your upload: `GET /api/config/list` (auto-scans `~/.jarvis` for `*.json`), `GET`/`PUT /api/config/file/:name/raw`, `POST /api/json/organize`. Path-traversal protected, known files (`commands.json`, `ai_config.json`, `memory.json`) keep shape validation, unknown files just get "must be valid JSON."

**Frontend (this patch — this was the actual missing 20%):**
- **Shared collapsible JSON tree component** (`buildJsonTree` in `app.js`) — one implementation used in two places:
  1. Read-only, for the organize-json chat bubble result.
  2. Editable, for the Settings Config tab — click a value to edit inline (type-aware: `true`/`false`/`null`/numbers coerce to their real JSON type, everything else stays a string), click an object key to rename it, `×` to delete a key/item, "+ add key" / "+ add item" buttons per branch, collapse/expand per node (first two levels open by default).
- **`organize-json <path>` chat-bubble integration** — typing this into the Ask box is intercepted in the `#ask-form` submit handler *before* it ever reaches `wsSend({type:"ask"...})`. It hits `POST /api/json/organize` directly instead. The echoed "You" bubble deliberately has no Redo button (Redo normally resends through the AI pipeline — that would defeat the whole "zero tokens" point). Renders a Raw JSON / Organized toggle in the reply bubble; invalid JSON shows the error + snippet instead of a tree, with no toggle.
- **Settings modal fully rewired to be generic** — this was the main gap. Your uploaded `index.html` already had the placeholder markup (`#settings-tabs`, `#settings-body`, empty) from an earlier session, but `app.js` still had the *old* hardcoded 5-tab `SETTINGS` object pointing at DOM ids (`#settings-json-commands` etc.) that no longer existed — so the whole modal was silently broken (every button referenced dead elements). Replaced with:
  - `openSettings()` fetches `/api/config/list` and builds tabs + panes dynamically — any new `*.json` file dropped into `~/.jarvis` shows up with zero code changes.
  - Each pane: hint text, path, Organized/Raw toggle, and either the editable tree or a raw `<textarea>`.
  - Switching Raw → Organized re-parses the current text (so hand-edits in Raw aren't lost); if it's invalid JSON, switching is blocked with an inline error instead of silently discarding the tree.
  - Save sends whichever mode's content is current; validates JSON first either way.
  - `commands.json` save still refreshes the command list (`loadCommands`) and a live `commands` broadcast from the server while the modal is open now updates that tab in place (`syncCommandsIntoSettingsTab`) instead of hitting the dead code the old `applyCommandsToUi` had.
- `Api` object: added `configList`, `getConfigFile`, `putConfigFile`, `organizeJson`; removed the five now-dead per-file methods (`getRaw`/`putRaw`/`getAiRaw`/... — server no longer serves those routes at all).
- `style.css` — new `.json-tree*` rules (tree rows, toggle, editable key/value, add/delete controls), `.json-org-*` rules (chat-bubble toggle + view), `.settings-pane__*` rules (hint/path/toggle/view wrapper). Reused existing design tokens (`--accent`, `--text-dim`, `--font-mono`, etc.) and the existing `.tab-btn`/`.debug-toggle-btn` visual language rather than inventing new button styles. One deliberate two-class selector (`.ask-msg__actions.ask-msg__actions--static`) to beat the later, equal-specificity `.ask-msg__actions{opacity:0.35}` rule — same gotcha called out in your style.css notes.

---

## Things NOT done / known gaps

- **No automated test suite exists in this repo** (confirmed by inspection — nothing under `jarvis-cli/` or `web/` looks like a test runner), so "verified" above means manual `py_compile`/`node --check`/`git apply --check` plus hand-running `organize-json` against sample files, not a CI pass. If you add pytest or similar later, `json_tools.py`'s pure functions (`read_json_file`, `render_tree`, `_error_snippet`) are easy unit-test targets.
- **The editable tree has no undo** — deleting a key or renaming one is immediate and only recoverable via the Reload button (which discards all unsaved edits and re-fetches from disk). No confirmation dialog on delete either, by design (kept it lightweight like the rest of the tree), but flagging it in case you want one.
- **No dirty-state warning on close** — closing the Settings modal (X, Cancel, click-outside, Escape) with unsaved tree/raw edits discards them silently, same as the original hardcoded version did. Not new behavior, just carried over.
- **Array reordering isn't supported** in the editable tree — you can add/remove/edit items but not drag-reorder them. Wasn't asked for; flagging since it's a natural next ask.
- **Large JSON files**: the tree renders the whole thing in the DOM (no virtualization/pagination). Fine for typical config files; a multi-MB JSON blob organized via the chat shortcut would be slow to render (though still zero-token, since that cost is local rendering, not the API call).
- **`organize-json` target-path resolution**: relative paths resolve against the user's home directory (same convention as `write_file`), not the current working directory of wherever `jarvis` was invoked from. Worth double-checking this matches what you actually want when running the CLI from inside a project folder.
- I did not touch `playnitebridge/`, any of the other tool modules (`spotify_*`, `playnite_*`, `radio_tools.py`, etc.), or `commands_config.py` — out of scope for this ask, untouched in the patch.
- Per the project's own "Known simplifications" list (unchanged by this patch, still open if you want them later): `risk_review` doesn't let you pin a specific provider for review; `showAskScreenshot` isn't buffered per-conversation; the confirm/stdin protocol only supports one child process per websocket connection.

---

## Plans / suggested next steps if you keep going

1. **Confirm this patch applies to your actual current repo state**, not just the zip you sent me — per the project's own history, the repo has been re-uploaded ~5 times and drifted each time. If your real `~/.jarvis` or repo folder has diverged since this upload, re-export a fresh zip and I'll re-diff against that before trusting this patch blindly.
2. If you want tree reordering (drag array items), it slots into `buildJsonTree`'s `buildChildren` — each row would need `draggable="true"` plus a dragover/drop handler that splices the array and calls `onChange()`/re-render, mirroring the existing step-card drag-reorder pattern already in `app.js`/`style.css` (`.step-card[draggable]`) for the command builder — reuse that pattern rather than inventing a new one.
3. If you want a dirty-state warning on modal close, track it off the existing `st.dirty` flag that's already being set on every edit (tree edit, raw textarea input) — just wasn't wired to a confirm prompt in this patch.
4. If large JSON becomes a real use case, look at virtualizing `buildJsonTree`'s rendering (only mount visible rows) rather than rewriting the collapse logic — the expand/collapse state (`collapsedPaths`/`expandedPaths` Sets) already tracks per-path state independent of what's actually rendered, so virtualization can sit on top of it without a redesign.
5. Consider adding a lightweight test file (even just a plain Python script, matching this repo's no-framework style) for `json_tools.py`'s error-snippet formatting — it's the one piece of new logic with actual edge cases (multi-line files, errors on line 1, unicode content).

---

## Tasks / methods reference (what changed, where)

| File | What changed |
|---|---|
| `jarvis-cli/jarvis/json_tools.py` | **New.** `resolve_path`, `read_json_file`, `render_tree`, plus private helpers (`_error_snippet`, `_type_name`, `_format_scalar`, `_child_entries`, `_branch_suffix`, `_label_for`, `_render_children`). |
| `web/public/app.js` — `Api` object | Removed `getRaw/putRaw/getAiRaw/putAiRaw/getPlayniteRaw/putPlayniteRaw/getSpotifyRaw/putSpotifyRaw/getMemoryRaw/putMemoryRaw`. Added `configList`, `getConfigFile`, `putConfigFile`, `organizeJson`. |
| `web/public/app.js` — `api()` helper | Thrown errors now carry `.data` (the full JSON error payload from the server), not just `.message` — needed so the organize-json bubble can show `line`/`column`/`snippet`, not just a flat string. |
| `web/public/app.js` — new section "Shared JSON tree component" | `jsonTreeTypeOf`, `jsonTreeIsContainer`, `jsonTreeCountLabel`, `jsonTreeScalarLabel`, `jsonTreeCoerce`, `buildJsonTree` (the main entry point — call with a mount element, the parsed value, and `{editable, onChange}`). |
| `web/public/app.js` — ask-form submit handler | Now checks for `organize-json <path>` *before* the normal ask/wsSend path; added `addOrganizeJsonUserBubble`, `addOrganizeJsonPendingBubble`, `renderOrganizeJsonResult`, `handleOrganizeJsonCommand`. |
| `web/public/app.js` — `applyCommandsToUi` | Old dead reference to `#settings-json-commands` replaced with a call to `syncCommandsIntoSettingsTab(state.commands)`. |
| `web/public/app.js` — Settings modal section (full rewrite) | `settingsFileMeta`, `settingsPaneEl`, `clearSettingsErrors`, `buildSettingsTabs`, `buildSettingsPanes`, `setActiveSettingsTabUi`, `renderSettingsPaneContent`, `setSettingsPaneMode`, `selectSettingsTab`, `openSettings`, `closeSettings`, `syncCommandsIntoSettingsTab`, plus the `#btn-settings*` event listeners rewired to the above instead of the old static `SETTINGS` map. |
| `web/public/style.css` | Added `.json-tree*` (tree component), `.json-org-*` (chat-bubble organize view), `.settings-pane__*`/`.settings-view-toggle`/`.settings-tree`/`.settings-empty` (Settings modal), `.ask-msg__actions.ask-msg__actions--static` (specificity fix for the bubble's static Copy-JSON button). No existing rules removed or renamed. |

No changes were made to `server.js`, `index.html`, `cli.py`, or any other backend tool module — they were already correct in your upload.
