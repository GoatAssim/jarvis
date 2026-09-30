# `/` command palette - manual testing checklist

What to click through by hand. The engine's logic (parsing, routing, every verb's
handler, passthrough argv handling, confirm gates, the key model) is covered by
`node tests/verify_slash_palette.js` and `python3 tests/test_slash_coverage.py`
against a fake DOM. This list is the part a fake DOM can't tell you: how it looks,
that it floats instead of shoving things around, real focus and key behaviour, and
that each verb does the real thing in the real UI.

This is the palette half of master plan **I.5 (Acceptance checklist)**, expanded.
The code-block/copy half (I.1) is not built - see the last section.

Test in **both** layouts (classic and focus) where noted, and once at phone width.

---

## 0. Before you start

- [ ] `python3 tests/test_slash_coverage.py` - all pass
- [ ] `python3 tests/test_reserved_names.py` - all pass
- [ ] `node tests/verify_slash_palette.js` - all pass
- [ ] Restart the web server (the new `POST /api/slash/run` route is server-side) and hard-refresh
      the page (three new/changed front-end files are cached otherwise)
- [ ] Browser console shows **no** `slash-palette.js:` warning on load (one would mean a verb has
      no handler, or the data file didn't load)
- [ ] Have at least: 2+ chats (one from Discord/Scheduled if you can), 1+ saved command (ideally one
      with a required `--var` and one with a default), 1+ installed skill (ideally one invalid, one
      loaded), 1+ registered service

## 1. Opening, floating, closing

- [ ] Type `/` in an empty Ask box: the palette appears **above** the composer
- [ ] The composer and the thread **do not move or resize** when it opens, filters or closes
- [ ] It spans the composer's width, sits *under* any modal, and scrolls inside itself when long
- [ ] Six group headings in this order: Chat · Model · Skills · Run · Panels · View, then a "More" row
- [ ] **Every** verb row shows a risk badge: `/clear` **dangerous** (red), `/run` and `/daemon`
      **caution**, everything else a quiet **safe**
- [ ] The footer shows, for the highlighted row: one line saying what Enter will do, the risk badge,
      an example, "asks before running" where it applies, and `local · no tokens` at the right
- [ ] `Esc` closes the palette and the Ask panel **stays open**; a **second** `Esc` closes the panel as it
      always did
- [ ] Clicking elsewhere closes it; tabbing out closes it
- [ ] Backspacing the `/` away closes it; `hello /new` (slash not first) never opens it
- [ ] `Shift+Enter` (making the text multi-line) closes it
- [ ] The placeholder reads "...type / for commands, or highlight text above to quote it"
- [ ] The old `/skillload` dropdown is gone: `/skillload ` shows the new palette
- [ ] Narrow to ~560px: still fits, description column drops out of rows, footer still describes the
      highlighted row. Very short window: it scrolls instead of running off the top
- [ ] Phone/touch width: rows are at least 40px tall

## 2. Keyboard

With the palette open:

- [ ] `↓` / `↑` move the highlight, skip headings, wrap at the ends
- [ ] `PageDown`/`PageUp` jump ~6 rows; `Home`/`End` go to first/last; `Shift+Tab` goes up one
- [ ] The highlighted row scrolls into view; a stationary mouse does not fight the keyboard
- [ ] `Tab` **fills** the highlighted row and **never runs anything** - try it on `/gu` (fills `/guides`,
      Guides does not open)
- [ ] `/ne` + `Enter`: box becomes `/new`, **nothing runs**; a second `Enter` runs it
      (Chat-group verbs are always two-step: `/new` `/stop` `/clear` `/redo` `/copy`)
- [ ] `/gu` + `Enter`: **opens Guides on the first Enter** (plain-safe verbs with nothing to fill in run
      at once: panel openers, `/skills` `/skillmake` `/skilladd` `/config` `/skin` `/help`)
- [ ] `/cl` + `Enter` completes to `/clear` but does **not** clear; the second `Enter` asks first
- [ ] `/chat` + `Enter` completes to `/chat ` and shows the chat list
- [ ] A typo such as `/nwe` matches nothing in the list ("No command "/nwe" matches - Enter sends it
      as a message"), and `Enter` triggers the near-miss hint (section 7) rather than completing it
- [ ] With the palette closed, `Enter` still sends a normal message and `Shift+Enter` still makes a newline
- [ ] **IME:** with a Japanese/Chinese/Korean IME, pressing `Enter` to commit a composition while the palette
      is open does not submit or complete anything

## 3. Mouse and touch

- [ ] Hover moves the highlight
- [ ] **Clicking a row only fills the input**: click "/guides" - the box says `/guides`, Guides does *not*
      open, focus stays in the box; then `Enter` opens it
- [ ] Clicking a verb that takes an argument (`/chat`) fills `/chat ` and shows level 2
- [ ] Clicking a greyed-out row only explains why
- [ ] Scrolling the list with the wheel/touch does not close it

## 4. Filtering, ranking, emphasis

- [ ] The matched letters are emphasised in the row name (`/ne` -> **ne** in `/new`)
- [ ] `/tools` finds **`/debug`** ("alias /tools"); `/tests` -> checklist; `/sched` -> schedule; `/switch` -> chat
- [ ] Matching is case-insensitive (`/GUIDES`)
- [ ] A word from a description finds the verb (`/reminders` -> schedule) but three random letters do not
- [ ] Among equally good matches, the one you used most recently comes first (run `/skills`, then type `/sk`)
- [ ] The screen-reader live region (dev tools -> element `.slash-sr`) reads "35 commands", then a count
      or "No matching commands" as you type

## 5. The "More" row and every other command

- [ ] With an empty query, the last row is "Show the other 61 commands" (31 run from here, 30 can't).
      `End` then `Enter` (or a click, then `Enter`) expands it; again collapses it. Closing the palette resets it
- [ ] Expanded: a **CLI commands** section (31, enabled, tagged `cli`, each with its tier) and a
      **Can't run from chat** section (30, greyed, each with why)
- [ ] Typing a name finds the right ones: `/memory` lists the memory commands, `/spotify` shows
      `spotify-login` greyed with "an interactive OAuth flow that needs a terminal"
- [ ] Hovering a CLI row shows its description and arguments (browser tooltip); the footer shows the
      same for the highlighted row

## 6. Level 2 - arguments (live lists)

| Type | Expect |
|---|---|
| `/chat ` | header chip "/chat › pick a chat · N of N"; rows with relative time and an origin badge (Discord / Scheduled); the open chat marked ✓ "open"; type to filter |
| `/run ` | saved commands; each shows its `--vars` as `--x REQUIRED` or `--x=default`; ones needing input tagged "needs args" |
| `/run <name> ` | hint "--var value ..." (free text - nothing to pick) |
| `/provider ` | `auto` + providers; current ones ✓ |
| `/provider anthropic,` | after the comma it lists providers again (an ordered list) |
| `/think ` | off / low / medium / high / show / hide; current ✓ |
| `/capacity ` | the server's real modes; current ✓ |
| `/layout ` | classic / focus; current ✓ |
| `/skillload ` | installed skills; **already-loaded ones marked ✓ "loaded"**; an **invalid** one greyed with its reason |
| `/skillunload ` | only skills **currently loaded** for this chat, plus `--all` |
| `/daemon ` | start / stop / restart / status; then real service ids with a green/grey dot |
| `/help ` | every verb |
| `/organize-json ` | a hint that it wants a path |
| `/memory-recall ` (any CLI command) | one row: its description and `args: <query>` |

- [ ] A list you've opened before appears **immediately** (no blank "Loading..." flash) and refreshes behind it
- [ ] Stop the server or block the request, open a list: a row "Couldn't load the list" tagged **retry**;
      `Enter` retries
- [ ] An empty list says "Nothing to pick from" / "No skills are loaded"
- [ ] `Tab` fills an argument; for `/daemon` it moves on to the next one

## 7. Grammar and safety

- [ ] `//new is just a word` is sent to Jarvis as `/new is just a word` (one slash stripped)
- [ ] `/etc/hosts is odd` and `/qwertyuiop` are sent as ordinary messages
- [ ] `/nwe` is **blocked** with "did you mean /new?"; the identical text + `Enter` again **sends it as a
      message**; changing the text starts over. Also `/doctr` -> "did you mean /doctor"
- [ ] `/spotify-login` (real, can't run from chat) is blocked with its reason; a second identical `Enter` sends it
- [ ] With a quote attached (select text in a reply and quote it), typing `/new` sends a normal message
- [ ] A multi-line message starting with `/` is never a command; `/ new` (space after the slash) is prose

## 8. Every verb, end to end

**Chat**
- [ ] `/new` opens a fresh chat
- [ ] `/chat <title>` switches (unique partial title works; two matches -> "pick one from the list";
      unknown -> "No chat called...", text stays); `/switch` works
- [ ] `/clear`: **confirm dialog** first; Cancel keeps the history; Clear empties it
- [ ] `/stop` during a reply stops it; with nothing running -> "Nothing is running."
- [ ] `/redo` re-runs your last prompt (refused while a reply streams)
- [ ] `/copy` copies Jarvis's last reply (raw markdown); on an empty chat -> "Nothing to copy yet."

**Model**
- [ ] `/provider openai` updates the picker label and the next ask uses it; `a,b` shows the order; `auto`
      resets; an unknown name and `auto,openai` are refused
- [ ] `/think high` updates the Thinking control; `show`/`hide` toggle the trace; a bad level is refused
- [ ] `/capacity compact` changes the capacity button; an unknown mode is refused and lists the real ones

**Skills**
- [ ] `/skillload <name>`, then `/skillunload <name>` (and the second with a name that *isn't* loaded says
      so instead of reporting success), then `/skillunload --all` ("Every manually-loaded skill unloaded...")
- [ ] `/skillload <invalid>` is refused with its error
- [ ] `/skillmake` opens the manager on New, `/skilladd` on Import, `/skills` just opens it

**Run**
- [ ] `/run <cmd>` runs it (no confirm, same as the Commands panel's button); a required `--var` missing ->
      refused naming it; `--target /x`, `--target="two words"` and `--target=x` work; defaults fill in;
      an option the command doesn't have is refused; refused mid-reply
- [ ] `/daemon start|status <id>` never ask. `/daemon stop <id>` always asks. `/daemon restart <id>` asks when the
      service is up and just starts it when it's already down
- [ ] `/organize-json <path>` behaves like bare `organize-json <path>` (which must still work), works mid-reply,
      and the bubble shows what you typed

**Panels** (each opens the right one, also mid-reply)
- [ ] `/guides` `/debug` (`/tools`) `/checklist` (`/tests`) `/schedule` (`/sched`) `/mcp` `/ctools` `/channels`
      `/daemons` `/backlog` `/logsearch` `/setup` `/subagents` `/notifications` `/logs`
- [ ] `/config` opens Config; `/skin` opens Skin
- [ ] `/layout classic|focus` switches and persists across a reload
- [ ] `/help` lists everything; `/help run` shows just that one; `/help nope` says so

## 9. CLI passthrough (`/<cli-name> [args]`)

- [ ] `/version` prints the version as a toast; `/doctor` shows its report in a dialog
- [ ] Multi-line output opens a dialog with the text in a monospace block; a **failing** command shows its
      stderr and exit code in an error-styled dialog; a command with no output says "done"
- [ ] Arguments: `/memory-recall "two words" x` passes `two words` and `x` as two arguments. Try
      `/memory-recall $(whoami) ; echo hi` - the text is searched for literally and **nothing is executed**
- [ ] `caution`-tier ones (e.g. `/digest-on`) run without a dialog
- [ ] Every **dangerous**-tier one asks first, showing the exact command line: `/logs-clear`,
      `/console-clear`, `/sched-clear`, `/notify-clear`, `/notify-send <msg>`, `/mcp-call <server> <tool>`,
      `/conv-delete <id>`. Cancel sends nothing to the server (check the Network tab)
- [ ] `/conv-delete` of the chat you have open is refused ("switch to another one first"); deleting another
      refreshes the sidebar
- [ ] They also work while Jarvis is replying
- [ ] Server side: `curl -X POST localhost:<port>/api/slash/run -H 'content-type: application/json'
      -d '{"name":"daemon-run","args":[]}'` -> **400** ("isn't a command the palette can run"); so is a name
      not in the table, and more than 24 arguments

## 10. While Jarvis is replying

- [ ] Start a long reply, then type `/`: `/clear`, `/redo`, `/run` are **greyed** with "can't run while
      Jarvis is replying"; clicking one explains why
- [ ] Forcing one through with `Enter` is refused and the **text stays** in the box
- [ ] `/stop`, `/new`, `/chat`, `/think`, `/provider`, `/capacity`, `/layout`, `/copy`, `/organize-json`, `/daemon`,
      every panel opener and `/skillload` still work mid-reply

## 11. Confirmations

- [ ] The toolbar **Clear** button now asks first (it never did), through the same dialog as `/clear`; Cancel keeps everything
- [ ] The Daemons panel's own Stop/Restart already asked before this change; the typed `/daemon` matches it

## 12. Regressions - must not have changed

- [ ] A normal message sends with Enter; Shift+Enter is a newline; the textarea still auto-grows
- [ ] Bare `organize-json <path>` still works
- [ ] The provider picker, Thinking picker, capacity button and layout toggle work by hand and stay in sync
      with what you typed
- [ ] The per-message Redo and Copy buttons; quotes attach/remove; sending a quote with no text
- [ ] Sidebar New / search / switch / delete
- [ ] Skill manager: create / import / edit / remove
- [ ] Mic button; the Menu's panels all still open (Test Checklist, Debug, Daemons, Scheduled, MCP, Channels,
      Backlog, Log Search, Setup)
- [ ] With `slash-palette.js` blocked (dev tools -> block request URL) the composer still sends messages

## 13. Accessibility

- [ ] `#ask-input` has `role="combobox"`, `aria-controls="slash-palette-list"`, `aria-autocomplete="list"`, and
      `aria-expanded` flips as it opens/closes
- [ ] `aria-activedescendant` follows the highlighted row's id
- [ ] The list is `role="listbox"`, rows `role="option"` with `aria-selected`, greyed ones `aria-disabled`
- [ ] Two live regions: the footer (description of the highlighted row) and `.slash-sr` (the count)
- [ ] With a screen reader, opening the palette announces the count and moving announces the row

## 14. Backend

- [ ] `jarvis skills-loaded` -> `{"loaded": []}`; after `jarvis skillload <name> <conv>`,
      `jarvis skills-loaded <conv>` lists it; `GET /api/skills/loaded?conversationId=<id>` agrees
- [ ] `python build_tools/bump_build_version.py` has been run (this patch touches `cli.py` and
      `reserved_names.py`; the patch already includes the regenerated `build_info.py`)

---

## Not built / known limits (so you don't file these as bugs)

- **Commands contributed by custom tools (I.2.11 / K.6.7)** - not built. The plan says its contract is TBD and it
  should get its own tracking id. The registry format leaves room for it; the K.6.7 acceptance line
  ("an unregistered custom action shows as dangerous") is untestable until it exists.
- **Touch long-press for the hover tooltip (D-I4).** Hover shows a browser tooltip and the footer follows the
  highlighted row, but on a touchscreen there is no hover to move it, and a tap fills the row.
- **`/copy` copies the whole last reply only**; `/copy 2` (one code block) waits on Part I.1.
- **No inline backlog quick-add** (D-I5 deferred) - `/backlog` opens the panel.
- **`/skillunload` doesn't distinguish "this chat" from "all chats"**; it shows the merged set. The CLI's own
  habit of reporting success for a name that isn't loaded is unchanged (I-B3 at the CLI level); the palette just never
  offers or accepts one.
- **The popover only opens above the composer**; there is no flip-below.
- **Near-miss detection compares verbs, aliases and CLI names**, not saved-command names.
- The native `confirm()` used for chat deletion and daemon *remove* was left alone.
- **Code blocks with one-click copy (Part I.1) are a separate, unbuilt feature.** The whole "Code blocks"
  half of I.5 stays unticked.
