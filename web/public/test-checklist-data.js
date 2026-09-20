/* ============================================================================
 * test-checklist-data.js — the catalogue behind Menu → Test Checklist.
 *
 * THIS FILE IS THE CHECKLIST. One entry per tool in jarvis-cli: what it does,
 * how to test it (Ask prompts and Debug direct-runs), what a pass looks like,
 * what it needs, and what to be careful of.
 *
 * WHO EDITS IT
 * ------------
 * Whoever adds, renames, removes or changes a tool. See AGENTS.md ("Test
 * Checklist"): adding a tool without adding its entry here is an incomplete
 * change. A tool with no entry still appears in the menu (the live catalogue
 * is read from /api/tools) but only as a bare name marked NO CHECKLIST.
 *
 * WHAT IS *NOT* HERE
 * ------------------
 * Test RESULTS (status, notes, ticked steps). Those live only in the browser
 * (localStorage) — nothing is ever written to the CLI or to ~/.jarvis.
 *
 * FORMAT
 * ------
 * Everything between the JSON-BEGIN / JSON-END markers is strict JSON (double
 * quotes, no trailing commas, no comments): tests/test_checklist_coverage.py
 * parses it, and so does the browser.
 *
 *   group   one of the ids in "groups"
 *   does    one line: what the tool is for
 *   steps   ordered tests. {"ask": "<prompt to type in Ask>", "expect": "..."}
 *           or {"run": {<Debug arguments>}, "expect": "..."} for a direct run
 *           that bypasses the model. Editing a step's text resets its tick
 *           (ticks are keyed by the text), which is what you want.
 *   needs   optional. Things that must exist before the test can pass
 *   os      optional. "windows" if it only works there
 *   care    optional. Side effects to warn about before running
 *   watch   optional. Known gotchas / invariants worth checking while testing
 * ========================================================================= */

window.JARVIS_TEST_CHECKLIST = /*JSON-BEGIN*/ {
 "schema": 1,
 "updated": "2026-09-20",
 "groups": [
  {
   "id": "core",
   "label": "Core & system info",
   "blurb": "Read-only lookups: time, battery, Wi-Fi, disk, RAM."
  },
  {
   "id": "memory",
   "label": "Memory & recall",
   "blurb": "Long-term facts, and searching past conversations."
  },
  {
   "id": "capacity",
   "label": "Capacity mode",
   "blurb": "How much context Jarvis sends per ask."
  },
  {
   "id": "discovery",
   "label": "Tool discovery",
   "blurb": "How the model finds tools it wasn't offered up front."
  },
  {
   "id": "commands",
   "label": "Commands",
   "blurb": "Saved commands, chains, and arbitrary shell."
  },
  {
   "id": "files",
   "label": "Files",
   "blurb": "Find, open, reveal, write and present files."
  },
  {
   "id": "web",
   "label": "Web",
   "blurb": "Search and fetch public pages."
  },
  {
   "id": "clipboard",
   "label": "Clipboard",
   "blurb": "Read, set, clear and wait on the system text clipboard."
  },
  {
   "id": "browser",
   "label": "Browser control",
   "blurb": "A real, persistent Playwright browser session Jarvis can drive."
  },
  {
   "id": "desktop",
   "label": "Desktop automation",
   "blurb": "Keyboard, mouse, windows, screenshots, OCR clicking."
  },
  {
   "id": "vision",
   "label": "Vision",
   "blurb": "Answering questions about what is on screen."
  },
  {
   "id": "audio",
   "label": "Audio",
   "blurb": "Volume, mute and output device."
  },
  {
   "id": "system_control",
   "label": "System control",
   "blurb": "Radios, package managers and git."
  },
  {
   "id": "youtube",
   "label": "YouTube / yt-dlp",
   "blurb": "Info, formats and downloads."
  },
  {
   "id": "spotify",
   "label": "Spotify",
   "blurb": "Playback, search, queue and suggestions."
  },
  {
   "id": "playnite",
   "label": "Playnite",
   "blurb": "Game library control through the Playnite Bridge."
  },
  {
   "id": "calendar",
   "label": "Calendar",
   "blurb": "Jarvis's own calendar plus subscribed feeds."
  },
  {
   "id": "scheduling",
   "label": "Scheduling & reminders",
   "blurb": "Reminders, notifications, scheduled work and events."
  },
  {
   "id": "workspace",
   "label": "Backlog & daemons",
   "blurb": "Untimed work, background services, raw log search."
  },
  {
   "id": "skills",
   "label": "Skills",
   "blurb": "Reusable instruction folders loaded on demand."
  },
  {
   "id": "subagents",
   "label": "Subagents",
   "blurb": "Delegated work with isolated API keys."
  },
  {
   "id": "dev_agent",
   "label": "Dev agents",
   "blurb": "Reading, editing and building code."
  },
  {
   "id": "channels",
   "label": "Channels",
   "blurb": "Discord / Instagram people and owner notifications."
  },
  {
   "id": "mcp",
   "label": "MCP",
   "blurb": "External tool servers."
  }
 ],
 "tools": {
  "get_datetime": {
   "group": "core",
   "does": "Local date, time and timezone.",
   "steps": [
    {
     "ask": "What time is it right now?",
     "expect": "Local time plus timezone that match the PC clock. No web search is fired."
    },
    {
     "ask": "What's today's date and what day of the week is it?",
     "expect": "Correct date and weekday."
    },
    {
     "run": {},
     "expect": "JSON with date, time and timezone fields."
    }
   ]
  },
  "get_battery": {
   "group": "core",
   "does": "Battery percent and charging status.",
   "steps": [
    {
     "ask": "How much battery do I have left?",
     "expect": "Percent and charging state on a laptop. On a desktop it says there is no battery instead of inventing a number."
    },
    {
     "run": {},
     "expect": "has_battery=false on a desktop; percent + charging on a laptop."
    }
   ]
  },
  "get_wifi_info": {
   "group": "core",
   "does": "Current Wi-Fi SSID.",
   "steps": [
    {
     "ask": "What Wi-Fi network am I on?",
     "expect": "Current SSID. On Ethernet or disconnected it says so plainly."
    },
    {
     "run": {},
     "expect": "SSID field, or a clear not-connected result."
    }
   ]
  },
  "get_location": {
   "group": "core",
   "does": "Approximate city/region from the public IP.",
   "steps": [
    {
     "ask": "Roughly where am I right now?",
     "expect": "City/region, described as approximate (IP-based), never as GPS."
    }
   ],
   "needs": [
    "Internet (public-IP lookup)"
   ],
   "watch": [
    "A VPN will show the VPN's location — that is correct, not a bug."
   ]
  },
  "get_system_info": {
   "group": "core",
   "does": "OS, hostname, architecture and uptime.",
   "steps": [
    {
     "ask": "What are my system specs — OS, hostname and uptime?",
     "expect": "OS, hostname, architecture and uptime that match Task Manager / Settings."
    }
   ]
  },
  "get_disk_usage": {
   "group": "core",
   "does": "Main drive total, used and free space.",
   "steps": [
    {
     "ask": "How much space is left on my main drive?",
     "expect": "Total / used / free for the main drive, matching Explorer within rounding."
    }
   ]
  },
  "get_memory_usage": {
   "group": "core",
   "does": "RAM total, used and available.",
   "steps": [
    {
     "ask": "How much RAM am I using right now?",
     "expect": "Total / used / available that roughly match Task Manager's Memory tab."
    }
   ]
  },
  "memory_save": {
   "group": "memory",
   "does": "Saves a durable fact to long-term memory.",
   "steps": [
    {
     "ask": "Remember that my favourite editor is Neovim.",
     "expect": "Saves one fact and confirms. It survives a new chat and ai-clear."
    },
    {
     "ask": "Actually, from now on remember my favourite editor is VS Code.",
     "expect": "Updates the SAME slot (via key) instead of adding a duplicate."
    },
    {
     "ask": "Remember that my API key is sk-test-123.",
     "expect": "Declines or refuses to store it — the tool says never save passwords or API keys."
    }
   ],
   "care": "Writes to your real long-term memory. Forget the test facts afterwards."
  },
  "memory_forget": {
   "group": "memory",
   "does": "Deletes a saved fact by id, key or text query.",
   "steps": [
    {
     "ask": "Forget what you know about my favourite editor.",
     "expect": "Confirmation prompt first (confirm_required), then the fact is gone from memory_search."
    },
    {
     "run": {
      "query": "favourite editor"
     },
     "expect": "Confirmation card with a risk note appears BEFORE anything is deleted."
    }
   ],
   "care": "Deletes real memories — only aim it at the test fact."
  },
  "memory_search": {
   "group": "memory",
   "does": "Searches long-term memory by literal text AND meaning.",
   "steps": [
    {
     "ask": "What do you remember about my editor?",
     "expect": "Finds the saved fact."
    },
    {
     "ask": "Which tool do I like to write code in?",
     "expect": "A paraphrase still finds it (matches meaning, not just words)."
    },
    {
     "run": {
      "query": "editor"
     },
     "expect": "List of matching facts with ids/keys."
    }
   ]
  },
  "search_conversations": {
   "group": "memory",
   "does": "Full-text search across past conversations.",
   "steps": [
    {
     "ask": "Search our old chats for where we talked about Spotify.",
     "expect": "Matching turns with snippets and which conversation each came from."
    },
    {
     "ask": "Find that command I used last week.",
     "expect": "Uses the since/until window; does not search the current chat."
    },
    {
     "run": {
      "query": "spotify",
      "mode": "words",
      "limit": 5
     },
     "expect": "Snippets + conversation ids; empty result is clean, not an error."
    }
   ],
   "watch": [
    "Different from search_log_files: this searches what was SAID, that one searches log files."
   ]
  },
  "get_capacity_mode": {
   "group": "capacity",
   "does": "Reads the current capacity mode and every available mode.",
   "steps": [
    {
     "ask": "What capacity mode are you in, and which modes exist?",
     "expect": "Current mode plus the full list (400% / 150% / 100% / 50%)."
    },
    {
     "run": {},
     "expect": "JSON with the current mode and the modes list."
    }
   ]
  },
  "set_capacity_mode": {
   "group": "capacity",
   "does": "Changes Jarvis's real prompt capacity mode.",
   "steps": [
    {
     "ask": "Switch to the ultra compact mode to save tokens.",
     "expect": "Mode changes, and the reply does NOT claim it already applies — it starts next message."
    },
    {
     "ask": "Go back to 100% capacity.",
     "expect": "Restores the balanced default."
    },
    {
     "run": {
      "next": true
     },
     "expect": "Cycles to the next mode."
    }
   ],
   "care": "Changes the real global mode. Put it back to 100% when finished.",
   "watch": [
    "Raising capacity is refused while a local model is answering.",
    "The Debug panel's own capacity button is local-only — it does not call this tool."
   ]
  },
  "search_tools": {
   "group": "discovery",
   "does": "Finds relevant tools by keyword or group name.",
   "steps": [
    {
     "ask": "Which of your tools deal with Spotify?",
     "expect": "Model calls search_tools and the matches become callable on its NEXT reply."
    },
    {
     "run": {
      "query": "battery"
     },
     "expect": "Matches include get_battery."
    },
    {
     "run": {},
     "expect": "No query lists the tool groups."
    }
   ]
  },
  "get_tool_schema": {
   "group": "discovery",
   "does": "Returns the full argument schema for one named tool.",
   "steps": [
    {
     "ask": "What arguments does the spotify_play tool take?",
     "expect": "Lists uri / query / type and which are optional."
    },
    {
     "run": {
      "name": "spotify_play"
     },
     "expect": "Full parameter schema; unknown name returns a clean error."
    }
   ]
  },
  "search_commands": {
   "group": "commands",
   "does": "Searches or lists the user's saved commands.",
   "steps": [
    {
     "ask": "What saved commands do I have?",
     "expect": "Lists everything (query omitted)."
    },
    {
     "ask": "Do I have a command for cleaning up my downloads?",
     "expect": "Searches by keyword instead of guessing a command name."
    },
    {
     "run": {
      "query": ""
     },
     "expect": "Full list of saved commands."
    }
   ]
  },
  "run_command": {
   "group": "commands",
   "does": "Runs one saved jarvis command.",
   "steps": [
    {
     "ask": "Run my <command name> command.",
     "expect": "Confirmation prompt, then runs it with the right vars."
    },
    {
     "ask": "Run <command name> but don't give it its required value.",
     "expect": "Asks you for the missing var instead of calling the tool."
    }
   ],
   "needs": [
    "At least one saved command"
   ],
   "care": "Runs whatever the saved command does — pick a harmless one."
  },
  "run_chain": {
   "group": "commands",
   "does": "Runs several saved commands in sequence or in parallel.",
   "steps": [
    {
     "ask": "Run <command A> and then <command B>.",
     "expect": "Sequential ('then') segments, one confirmation."
    },
    {
     "ask": "Run <command A> and <command B> at the same time.",
     "expect": "Second segment uses mode 'and' (parallel)."
    }
   ],
   "needs": [
    "Two saved commands"
   ],
   "care": "Runs real commands — use harmless ones."
  },
  "create_command": {
   "group": "commands",
   "does": "Creates a new saved command in commands.json.",
   "steps": [
    {
     "ask": "Create a command called hello-test that runs `echo hello`.",
     "expect": "Confirmation first; afterwards it is in search_commands and in the Config editor."
    }
   ],
   "care": "Writes to commands.json. Delete hello-test afterwards."
  },
  "update_command": {
   "group": "commands",
   "does": "Edits an existing saved command.",
   "steps": [
    {
     "ask": "Rename the hello-test command to hello-test2 and make it run `echo hi`.",
     "expect": "Confirmation, then only the named fields change."
    }
   ],
   "needs": [
    "A saved command to edit (e.g. hello-test)"
   ],
   "care": "Edits commands.json — only touch the test command."
  },
  "run_custom_command": {
   "group": "commands",
   "does": "Runs an arbitrary shell command outside saved commands.",
   "steps": [
    {
     "ask": "Run this shell command: echo jarvis-test",
     "expect": "Confirmation plus a second-AI risk note (ai_review is on by default); output is 'jarvis-test'."
    },
    {
     "ask": "Run this shell command: echo del /f /s /q C:\\*",
     "expect": "Harmless echo — check the risk note flags the scary-looking pattern. Decline it and confirm nothing ran."
    }
   ],
   "care": "Arbitrary shell. Stick to echo-style commands.",
   "watch": [
    "Needs a second configured AI provider to produce a risk note; without one the prompt shows with no note."
   ]
  },
  "search_files": {
   "group": "files",
   "does": "Instant file/folder search through Everything.",
   "steps": [
    {
     "ask": "Find every PDF in my Downloads folder.",
     "expect": "Uses in_folder=downloads rather than searching the whole PC."
    },
    {
     "ask": "Where is a file called notes.txt?",
     "expect": "Returns paths; result count is capped by max_results."
    },
    {
     "run": {
      "query": "*.pdf",
      "in_folder": "downloads",
      "max_results": 5,
      "sort": "date_modified_desc"
     },
     "expect": "At most 5 PDFs, newest first."
    }
   ],
   "needs": [
    "Everything (voidtools) running in the background"
   ],
   "watch": [
    "A friendly folder name (desktop/documents/downloads/pictures/music/videos) comes from everything.json folder_aliases."
   ]
  },
  "reveal_in_explorer": {
   "group": "files",
   "does": "Opens Explorer with a file or folder highlighted.",
   "steps": [
    {
     "ask": "Show me where C:\\Windows\\notepad.exe is in Explorer.",
     "expect": "Explorer opens with notepad.exe highlighted."
    },
    {
     "run": {
      "path": "C:\\Windows\\notepad.exe"
     },
     "expect": "Highlighted in Explorer."
    }
   ],
   "os": "windows"
  },
  "open_file_location": {
   "group": "files",
   "does": "Opens the folder containing a path.",
   "steps": [
    {
     "run": {
      "path": "C:\\Windows\\notepad.exe"
     },
     "expect": "The Windows folder opens, nothing pre-selected."
    },
    {
     "run": {
      "path": "C:\\Windows"
     },
     "expect": "A folder path opens directly."
    }
   ],
   "os": "windows"
  },
  "open_file": {
   "group": "files",
   "does": "Opens a file with its default application.",
   "steps": [
    {
     "ask": "Open <path to a .txt file>.",
     "expect": "Confirmation first, then it opens in the default app."
    },
    {
     "run": {
      "path": "<path to a .txt file>"
     },
     "expect": "Confirmation card, then the file opens. A folder path is rejected."
    }
   ],
   "care": "Opens a real file in a real app — use a text file."
  },
  "write_file": {
   "group": "files",
   "does": "Creates, overwrites or appends to a text file.",
   "steps": [
    {
     "ask": "Create a file called jarvis-test.txt on my Desktop containing 'hello'.",
     "expect": "Confirmation shows path + content; file appears with that content."
    },
    {
     "ask": "Append the line 'second line' to jarvis-test.txt on my Desktop.",
     "expect": "mode=append — original line kept."
    },
    {
     "run": {
      "path": "<existing file>",
      "content": "x",
      "mode": "create_only"
     },
     "expect": "Refuses because the file already exists."
    }
   ],
   "care": "Writes real files — stay on the Desktop test file."
  },
  "organize_json": {
   "group": "files",
   "does": "Shows a JSON file as an interactive, collapsible view.",
   "steps": [
    {
     "ask": "Organize this JSON file: <path to a .json file>.",
     "expect": "Chat shows a collapsible Organized view with a Raw JSON toggle. The model only gets ok/type/count."
    },
    {
     "ask": "Check this JSON file, I think it's broken: <path to a malformed .json>.",
     "expect": "Reports the file is invalid instead of rendering it."
    }
   ]
  },
  "present_file": {
   "group": "files",
   "does": "Shows a file or folder as a card with Open / Reveal / Download.",
   "steps": [
    {
     "ask": "Show me the file at <path> as a card.",
     "expect": "Card with name, type, size, path and Open / Reveal (web UI also Download)."
    },
    {
     "ask": "Present the folder <folder path>.",
     "expect": "Folder card; Download zips it first."
    }
   ]
  },
  "web_search": {
   "group": "web",
   "does": "Searches the public web (DuckDuckGo).",
   "steps": [
    {
     "ask": "What's the latest stable version of Node.js?",
     "expect": "Searches, then fetches 1–3 sources and cites their URLs. No invented sources."
    },
    {
     "run": {
      "query": "jarvis ai assistant",
      "limit": 3
     },
     "expect": "Up to 3 title/url/snippet results."
    }
   ],
   "needs": [
    "Internet"
   ]
  },
  "web_fetch": {
   "group": "web",
   "does": "Fetches a public page as extracted text.",
   "steps": [
    {
     "ask": "Summarize https://example.com for me.",
     "expect": "Summary that matches the page."
    },
    {
     "run": {
      "url": "https://example.com"
     },
     "expect": "Extracted text, capped around 4k characters."
    }
   ],
   "needs": [
    "Internet"
   ],
   "watch": [
    "Output is capped at ~4k characters — long pages are truncated by design."
   ]
  },
  "type_text": {
   "group": "desktop",
   "does": "Types a string into whatever has keyboard focus.",
   "steps": [
    {
     "ask": "Type 'hello world' into the window that's focused.",
     "expect": "Confirmation, then the text appears in the focused window."
    },
    {
     "run": {
      "text": "hello",
      "interval": 0.02
     },
     "expect": "Text appears with a slight delay between keys."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused."
  },
  "write_on_screen": {
   "group": "desktop",
   "does": "Types text, then presses Enter to submit.",
   "steps": [
    {
     "ask": "Focus Notepad and write 'hello' then press Enter.",
     "expect": "Text typed and a newline follows."
    },
    {
     "run": {
      "text": "hi",
      "press_enter": false
     },
     "expect": "Behaves like type_text (no Enter)."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused."
  },
  "press_key": {
   "group": "desktop",
   "does": "Presses a single key, optionally repeated.",
   "steps": [
    {
     "ask": "Press the down arrow 3 times.",
     "expect": "Cursor moves down three lines."
    },
    {
     "run": {
      "key": "esc"
     },
     "expect": "Escape is sent to the focused window."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused."
  },
  "hotkey": {
   "group": "desktop",
   "does": "Sends a key combination.",
   "steps": [
    {
     "ask": "Select all in the current window.",
     "expect": "Uses ctrl+a rather than clicking around."
    },
    {
     "run": {
      "keys": [
       "ctrl",
       "a"
      ]
     },
     "expect": "Everything in the focused Notepad is selected."
    },
    {
     "run": {
      "keys": "alt+tab"
     },
     "expect": "String form works too and switches windows."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused."
  },
  "scroll": {
   "group": "desktop",
   "does": "Scrolls the mouse wheel in a direction.",
   "steps": [
    {
     "run": {
      "amount": 5,
      "direction": "down"
     },
     "expect": "A long document/page scrolls down."
    },
    {
     "run": {
      "amount": 5,
      "direction": "up"
     },
     "expect": "…and back up."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused."
  },
  "move_mouse": {
   "group": "desktop",
   "does": "Moves the cursor to absolute coordinates.",
   "steps": [
    {
     "run": {
      "x": 400,
      "y": 300,
      "duration": 0.3
     },
     "expect": "Cursor glides to (400, 300)."
    },
    {
     "ask": "Move the mouse to 400, 300 and tell me where the cursor ended up.",
     "expect": "Reads the position back afterwards; it lands at about (400, 300)."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused.",
   "watch": [
    "Display scaling above 100% can make coordinates differ from what Windows Settings shows."
   ]
  },
  "click": {
   "group": "desktop",
   "does": "Clicks at coordinates or at the current cursor position.",
   "steps": [
    {
     "run": {
      "x": 400,
      "y": 300
     },
     "expect": "Confirmation, then a left-click at that point."
    },
    {
     "run": {
      "x": 400,
      "y": 300,
      "button": "right"
     },
     "expect": "Context menu opens."
    },
    {
     "run": {
      "clicks": 2
     },
     "expect": "Double-click at the current cursor position."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused. Aim it at empty desktop space."
  },
  "drag": {
   "group": "desktop",
   "does": "Drags from one point to another.",
   "steps": [
    {
     "run": {
      "x1": 300,
      "y1": 300,
      "x2": 500,
      "y2": 300,
      "duration": 0.5
     },
     "expect": "Confirmation, then a drag — selects text in Notepad if it starts on text."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused."
  },
  "get_screen_size": {
   "group": "desktop",
   "does": "Primary screen width and height in pixels.",
   "steps": [
    {
     "ask": "What's my screen resolution?",
     "expect": "Matches your primary display (mind display scaling)."
    },
    {
     "run": {},
     "expect": "width / height in pixels."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ]
  },
  "get_mouse_position": {
   "group": "desktop",
   "does": "Current mouse cursor position.",
   "steps": [
    {
     "run": {},
     "expect": "x / y that match where the cursor is; move it and re-run to see the change."
    }
   ],
   "needs": [
    "pyautogui installed (pip install pyautogui)"
   ]
  },
  "list_windows": {
   "group": "desktop",
   "does": "Lists open windows with position, size and state.",
   "steps": [
    {
     "ask": "What windows do I have open?",
     "expect": "Titles match your taskbar; each has position/size/state."
    },
    {
     "run": {},
     "expect": "Full JSON list."
    }
   ],
   "needs": [
    "pygetwindow installed (ships with pyautogui; best supported on Windows)"
   ]
  },
  "focus_window": {
   "group": "desktop",
   "does": "Brings a window to the front by partial title.",
   "steps": [
    {
     "ask": "Bring Notepad to the front.",
     "expect": "Notepad comes forward."
    },
    {
     "run": {
      "title": "Notepad"
     },
     "expect": "Window activates."
    },
    {
     "run": {
      "title": "zzz-no-such-window"
     },
     "expect": "Clean not-found result, no crash."
    }
   ],
   "needs": [
    "pygetwindow installed (ships with pyautogui; best supported on Windows)"
   ],
   "watch": [
    "Windows sometimes refuses to activate a window from a background process."
   ]
  },
  "get_active_window": {
   "group": "desktop",
   "does": "Title/position/size of the focused window.",
   "steps": [
    {
     "run": {},
     "expect": "Reports the window that had focus."
    }
   ],
   "needs": [
    "pygetwindow installed (ships with pyautogui; best supported on Windows)"
   ],
   "watch": [
    "Focus may already be on the browser when this runs — check you're reading the right window."
   ]
  },
  "get_window_size": {
   "group": "desktop",
   "does": "Position/size rectangle of one window.",
   "steps": [
    {
     "run": {
      "title": "Notepad"
     },
     "expect": "left/top/width/height for Notepad."
    }
   ],
   "needs": [
    "pygetwindow installed (ships with pyautogui; best supported on Windows)"
   ]
  },
  "get_window_info": {
   "group": "desktop",
   "does": "Full title/position/size/active/minimized state for one window.",
   "steps": [
    {
     "run": {
      "title": "Notepad"
     },
     "expect": "Includes active and minimized flags."
    },
    {
     "ask": "Minimise Notepad, then tell me whether the Notepad window is minimized.",
     "expect": "Reports minimized=true; restore it and ask again to see it flip back."
    }
   ],
   "needs": [
    "pygetwindow installed (ships with pyautogui; best supported on Windows)"
   ]
  },
  "take_screenshot": {
   "group": "desktop",
   "does": "Captures the screen and shows it in the Jarvis UI.",
   "steps": [
    {
     "ask": "Take a screenshot.",
     "expect": "Image card shows in the Jarvis UI. The model only receives an ok/path result, not the image."
    }
   ],
   "needs": [
    "Windows PowerShell, or mss + Pillow installed"
   ]
  },
  "click_on_text": {
   "group": "desktop",
   "does": "Finds on-screen text via local OCR and clicks it.",
   "steps": [
    {
     "run": {
      "text": "File",
      "click": false
     },
     "expect": "Finds the text without clicking — safe dry run."
    },
    {
     "ask": "Click the 'File' menu in Notepad.",
     "expect": "Confirmation + AI review note, then it clicks File."
    },
    {
     "ask": "Click 'a' on the screen.",
     "expect": "If several matches are similarly confident, NOTHING is clicked and the candidates come back."
    }
   ],
   "needs": [
    "Tesseract OCR installed (local OCR — nothing leaves the PC)"
   ],
   "care": "Moves the real mouse/keyboard. Open Notepad (or another harmless target) first and keep it focused."
  },
  "read_screen": {
   "group": "desktop",
   "does": "Reads on-screen text with local OCR.",
   "steps": [
    {
     "ask": "Read what's on my screen.",
     "expect": "Plain text lines from the visible windows — no image is sent anywhere."
    },
    {
     "run": {},
     "expect": "Recognised text lines."
    }
   ],
   "needs": [
    "Tesseract OCR installed (local OCR — nothing leaves the PC)"
   ]
  },
  "look_at_screen": {
   "group": "vision",
   "does": "Answers a question about the screen, using text first and pixels only if needed.",
   "steps": [
    {
     "ask": "What app is in focus right now?",
     "expect": "Answered from screen text (the free path)."
    },
    {
     "ask": "What colour is my taskbar?",
     "expect": "Text can't answer this, so it falls back to actually looking at the pixels."
    },
    {
     "run": {
      "question": "Describe the layout",
      "force_vision": true
     },
     "expect": "Forces the pixel path."
    }
   ],
   "needs": [
    "Tesseract OCR installed (local OCR — nothing leaves the PC)",
    "A provider that can read images (for the pixel path)"
   ]
  },
  "audio_status": {
   "group": "audio",
   "does": "Default output device, volume, mute state and other outputs.",
   "steps": [
    {
     "ask": "What's my volume, and which output device am I using?",
     "expect": "Matches the Windows sound settings."
    }
   ],
   "os": "windows"
  },
  "set_volume": {
   "group": "audio",
   "does": "Sets the system volume to an absolute percent.",
   "steps": [
    {
     "ask": "Set my volume to 30%.",
     "expect": "Volume slider lands on 30 and it unmutes if muted."
    },
    {
     "run": {
      "percent": 0
     },
     "expect": "Boundary value works."
    }
   ],
   "os": "windows",
   "care": "Audible — check speakers before testing at high values."
  },
  "volume_up": {
   "group": "audio",
   "does": "Raises the system volume.",
   "steps": [
    {
     "ask": "Turn the volume up a bit.",
     "expect": "Rises by the default 10 points."
    },
    {
     "run": {
      "amount": 5
     },
     "expect": "Rises by exactly 5."
    }
   ],
   "os": "windows"
  },
  "volume_down": {
   "group": "audio",
   "does": "Lowers the system volume.",
   "steps": [
    {
     "ask": "Turn it down a bit.",
     "expect": "Drops by the default 10 points."
    },
    {
     "run": {
      "amount": 5
     },
     "expect": "Drops by exactly 5."
    }
   ],
   "os": "windows"
  },
  "set_mute": {
   "group": "audio",
   "does": "Mutes, unmutes or toggles the output.",
   "steps": [
    {
     "ask": "Mute my PC.",
     "expect": "Mute icon appears."
    },
    {
     "ask": "Unmute.",
     "expect": "Sound returns."
    },
    {
     "run": {
      "action": "toggle"
     },
     "expect": "Flips whichever state it was in."
    }
   ],
   "os": "windows"
  },
  "set_default_output": {
   "group": "audio",
   "does": "Switches the default output device by partial name.",
   "steps": [
    {
     "ask": "Switch my audio output to headphones.",
     "expect": "Calls audio_status first if unsure, then switches; sound moves."
    },
    {
     "run": {
      "device": "<part of a device name>"
     },
     "expect": "Default device changes; unknown name returns a clean error."
    }
   ],
   "needs": [
    "Two or more output devices"
   ],
   "os": "windows"
  },
  "radio_status": {
   "group": "system_control",
   "does": "Wi-Fi and Bluetooth radio on/off state.",
   "steps": [
    {
     "ask": "Are my Wi-Fi and Bluetooth turned on?",
     "expect": "Both states match the Windows quick settings."
    }
   ],
   "os": "windows"
  },
  "wifi_set": {
   "group": "system_control",
   "does": "Turns Windows Wi-Fi on or off.",
   "steps": [
    {
     "run": {
      "action": "on"
     },
     "expect": "Wi-Fi stays/turns on. May need Administrator."
    },
    {
     "ask": "Turn off my Wi-Fi.",
     "expect": "Must NOT switch off silently — off requires confirm=true and a confirmation."
    }
   ],
   "os": "windows",
   "care": "Turning Wi-Fi off cuts cloud-model access. Have a wired or local-model fallback and turn it back on.",
   "watch": [
    "May need Administrator."
   ]
  },
  "bluetooth_set": {
   "group": "system_control",
   "does": "Turns Windows Bluetooth on or off.",
   "steps": [
    {
     "run": {
      "action": "on"
     },
     "expect": "Bluetooth stays/turns on. May need Administrator."
    },
    {
     "ask": "Turn off Bluetooth.",
     "expect": "Requires confirm=true plus a confirmation before it switches off."
    }
   ],
   "os": "windows",
   "care": "Will disconnect Bluetooth headsets/mice. Turn it back on afterwards.",
   "watch": [
    "May need Administrator."
   ]
  },
  "package_managers": {
   "group": "system_control",
   "does": "Lists installed Windows package managers.",
   "steps": [
    {
     "ask": "Which package managers do I have installed?",
     "expect": "Only real installs (winget, choco, scoop, pip, pipx, npm) are listed."
    }
   ],
   "os": "windows"
  },
  "package_search": {
   "group": "system_control",
   "does": "Searches installed package managers for software.",
   "steps": [
    {
     "ask": "Search for 7zip in the package managers.",
     "expect": "Results from each installed manager with real package ids."
    },
    {
     "run": {
      "query": "7zip",
      "manager": "winget"
     },
     "expect": "winget-only results."
    }
   ],
   "os": "windows",
   "watch": [
    "Must run before any install — it should never invent package ids."
   ]
  },
  "package_info": {
   "group": "system_control",
   "does": "Details for one exact package id.",
   "steps": [
    {
     "run": {
      "manager": "winget",
      "package": "7zip.7zip"
     },
     "expect": "Version/publisher details for that id."
    }
   ],
   "os": "windows"
  },
  "package_list": {
   "group": "system_control",
   "does": "Lists installed packages for one manager.",
   "steps": [
    {
     "run": {
      "manager": "pip",
      "query": "requests"
     },
     "expect": "Installed pip packages filtered by 'requests'."
    }
   ],
   "os": "windows"
  },
  "package_install": {
   "group": "system_control",
   "does": "Installs a package (needs confirm=true).",
   "steps": [
    {
     "ask": "Install 7zip.",
     "expect": "Searches first, then asks you to agree to the exact manager + id before installing."
    },
    {
     "run": {
      "manager": "pip",
      "package": "cowsay",
      "confirm": true
     },
     "expect": "Confirmation card, then installs. Use a harmless package."
    }
   ],
   "os": "windows",
   "care": "Installs real software. Use pip 'cowsay' and uninstall it after."
  },
  "package_uninstall": {
   "group": "system_control",
   "does": "Uninstalls a package (needs confirm=true).",
   "steps": [
    {
     "run": {
      "manager": "pip",
      "package": "cowsay",
      "confirm": true
     },
     "expect": "Confirmation card, then removes the test package."
    }
   ],
   "os": "windows",
   "care": "Uninstalls real software — only the package you installed to test."
  },
  "git_run": {
   "group": "system_control",
   "does": "Runs an allowlisted git subcommand in a repo.",
   "steps": [
    {
     "ask": "Show git status for <repo path>.",
     "expect": "status output for that repo (cwd honoured)."
    },
    {
     "run": {
      "command": "log",
      "args": [
       "-n",
       "5",
       "--oneline"
      ],
      "cwd": "<repo path>"
     },
     "expect": "Last five commits."
    },
    {
     "run": {
      "command": "reset",
      "args": [
       "--hard"
      ],
      "cwd": "<repo path>"
     },
     "expect": "Refused until confirm=true — cancel it at the prompt."
    },
    {
     "run": {
      "command": "rm"
     },
     "expect": "Not on the allowlist — rejected ('not a general shell')."
    }
   ],
   "care": "Do this in a scratch folder or throwaway repo, not a real project.",
   "watch": [
    "reset / clean / force-push / clone need confirm=true."
   ]
  },
  "git_commit_all": {
   "group": "system_control",
   "does": "Stages everything and commits in one call.",
   "steps": [
    {
     "ask": "Commit everything in <repo path> with the message 'test commit'.",
     "expect": "One tool round (add -A + commit), not four separate git_run calls."
    }
   ],
   "care": "Makes a real commit. Do this in a scratch folder or throwaway repo, not a real project."
  },
  "ytdl_info": {
   "group": "youtube",
   "does": "Video/audio metadata without downloading.",
   "steps": [
    {
     "ask": "Get info on this video: <url>",
     "expect": "Title, uploader, duration, qualities and subtitle languages; nothing downloaded."
    },
    {
     "run": {
      "url": "<video url>"
     },
     "expect": "Includes an ffmpeg_available flag."
    }
   ],
   "needs": [
    "Internet",
    "yt-dlp installed"
   ]
  },
  "ytdl_formats": {
   "group": "youtube",
   "does": "Lists every raw format for a URL, best first.",
   "steps": [
    {
     "run": {
      "url": "<video url>"
     },
     "expect": "format_id / ext / resolution / codecs, best quality first."
    },
    {
     "ask": "What exact formats are available for <url>?",
     "expect": "Presents formats and offers quality=\"id:<format_id>\" for the download."
    }
   ],
   "needs": [
    "Internet",
    "yt-dlp installed"
   ]
  },
  "ytdl_download": {
   "group": "youtube",
   "does": "Downloads a video or extracts audio, then offers the file.",
   "steps": [
    {
     "ask": "Download the audio of <url> as mp3.",
     "expect": "Confirmation, then a file card/download appears. mp3 needs ffmpeg."
    },
    {
     "ask": "Download <url> as an mp4.",
     "expect": "Confirmation, then an mp4."
    },
    {
     "ask": "Download this playlist: <playlist url>.",
     "expect": "playlist=true and it stops at the 10-item hard cap."
    }
   ],
   "needs": [
    "Internet",
    "yt-dlp installed",
    "ffmpeg for mp3 conversion, merging and embedding"
   ],
   "care": "Writes files to disk — set output_dir to a scratch folder."
  },
  "spotify_open": {
   "group": "spotify",
   "does": "Launches the Spotify desktop app.",
   "steps": [
    {
     "ask": "Open Spotify.",
     "expect": "App launches (not via run_command). It does not start a song."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_now": {
   "group": "spotify",
   "does": "Account, now playing, pause state and devices.",
   "steps": [
    {
     "ask": "What's playing on Spotify right now?",
     "expect": "Track, artist, paused/playing and the devices on this PC."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_search": {
   "group": "spotify",
   "does": "Searches the user's Spotify catalogue.",
   "steps": [
    {
     "ask": "Search Spotify for Daft Punk.",
     "expect": "Compact name/artists/uri results."
    },
    {
     "run": {
      "query": "Daft Punk",
      "type": "artist"
     },
     "expect": "Artist results only."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_play": {
   "group": "spotify",
   "does": "Plays a track, playlist, album or artist.",
   "steps": [
    {
     "ask": "Play Get Lucky on Spotify.",
     "expect": "Playback actually starts. It only claims success if the tool returned ok."
    },
    {
     "run": {
      "query": "Get Lucky",
      "type": "track"
     },
     "expect": "Search-then-play in one call."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_control": {
   "group": "spotify",
   "does": "Pause, resume, skip, shuffle, repeat and volume.",
   "steps": [
    {
     "ask": "Pause the music.",
     "expect": "Playback pauses."
    },
    {
     "ask": "Skip to the next song.",
     "expect": "Next track plays."
    },
    {
     "ask": "Set Spotify volume to 30.",
     "expect": "Volume changes to 30 (0–100)."
    },
    {
     "run": {
      "action": "shuffle_on"
     },
     "expect": "Shuffle toggles on."
    },
    {
     "run": {
      "action": "repeat_track"
     },
     "expect": "Repeat-track mode on."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_queue": {
   "group": "spotify",
   "does": "Adds a track to the queue.",
   "steps": [
    {
     "ask": "Queue up Instant Crush.",
     "expect": "Track added to the queue, and it plays after the current one."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_playlists": {
   "group": "spotify",
   "does": "Lists the account's playlists.",
   "steps": [
    {
     "ask": "List my Spotify playlists.",
     "expect": "Name, uri and length for each playlist."
    },
    {
     "run": {
      "limit": 5
     },
     "expect": "Exactly the first five."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_suggest": {
   "group": "spotify",
   "does": "Taste-based suggestions from the account.",
   "steps": [
    {
     "ask": "What should I listen to?",
     "expect": "Top tracks/artists, recently played, Made For You mixes and try-next ideas."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ]
  },
  "spotify_like": {
   "group": "spotify",
   "does": "Saves the current or a given track to Liked Songs.",
   "steps": [
    {
     "ask": "Like this song.",
     "expect": "Currently playing track lands in Liked Songs."
    }
   ],
   "needs": [
    "Spotify desktop app logged in",
    "Spotify API login done (jarvis spotify-login) with the SAME account"
   ],
   "care": "Changes your real Liked Songs — un-like it afterwards."
  },
  "list_subagent_roles": {
   "group": "subagents",
   "does": "Lists subagent roles and whether each has a key pool.",
   "steps": [
    {
     "ask": "What subagent roles do you have?",
     "expect": "Every role, and whether it has a key pool configured."
    },
    {
     "run": {},
     "expect": "JSON list of roles."
    }
   ]
  },
  "spawn_subagent": {
   "group": "subagents",
   "does": "Creates a subagent task with its own key, tools and budget.",
   "steps": [
    {
     "ask": "Spawn a subagent to <small scoped task>.",
     "expect": "Calls list_subagent_roles first if unsure; returns a task id immediately without running it."
    },
    {
     "run": {
      "role": "<a listed role>",
      "goal": "Say hello",
      "max_steps": 3
     },
     "expect": "Task id returned right away."
    }
   ],
   "needs": [
    "Subagent API key pool configured for the role you use"
   ],
   "watch": [
    "A subagent must never spend the main key — its key comes only from its own role pool."
   ]
  },
  "run_subagents": {
   "group": "subagents",
   "does": "Drives spawned subagents to completion and returns results.",
   "steps": [
    {
     "ask": "Spawn a subagent to <small task> and run it.",
     "expect": "Blocks until done/blocked/failed/round cap, then returns the combined result."
    },
    {
     "run": {
      "task_ids": [
       "<task id>"
      ],
      "max_rounds": 3
     },
     "expect": "Results for exactly that task."
    }
   ],
   "needs": [
    "Subagent API key pool configured for the role you use"
   ]
  },
  "subagent_status": {
   "group": "subagents",
   "does": "Checks a subagent's status without advancing it.",
   "steps": [
    {
     "run": {
      "task_id": "<task id>"
     },
     "expect": "Status only — the task does not progress."
    }
   ],
   "needs": [
    "Subagent API key pool configured for the role you use"
   ]
  },
  "playnite_list_game_actions": {
   "group": "playnite",
   "does": "Lists a game's launch actions.",
   "steps": [
    {
     "ask": "What launch options does <game> have?",
     "expect": "Includes the virtual LibraryPlugin action plus stored File/URL/Emulator/Script actions."
    },
    {
     "run": {
      "name": "<game>"
     },
     "expect": "Actions with ids."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_launch_action": {
   "group": "playnite",
   "does": "Launches one specific game action.",
   "steps": [
    {
     "ask": "Launch the <action name> action for <game>.",
     "expect": "Confirms first, then starts exactly that action."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Starts a real game/process."
  },
  "playnite_find_game": {
   "group": "playnite",
   "does": "Finds ONE game by name substring.",
   "steps": [
    {
     "ask": "Find the game <game name>.",
     "expect": "At most ~5 compact results."
    },
    {
     "ask": "List all my games.",
     "expect": "Should NOT use find_game to dump the library — it should reach for query_games with filters."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_launch_game": {
   "group": "playnite",
   "does": "Plays a game like Play in Playnite.",
   "steps": [
    {
     "ask": "Play <game>.",
     "expect": "Game starts through its library integration or stored play action."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Starts a real game."
  },
  "playnite_library_stats": {
   "group": "playnite",
   "does": "Library stats overview.",
   "steps": [
    {
     "ask": "Give me an overview of my game library.",
     "expect": "Counts/totals, not a full game list."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_get_game": {
   "group": "playnite",
   "does": "One game by id.",
   "steps": [
    {
     "run": {
      "game_id": "<id>",
      "detail": "compact"
     },
     "expect": "Compact record including the actions array with ids."
    },
    {
     "run": {
      "game_id": "<id>",
      "detail": "full"
     },
     "expect": "Full record."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_update_game": {
   "group": "playnite",
   "does": "Updates a game's metadata.",
   "steps": [
    {
     "ask": "Mark <game> as a favourite.",
     "expect": "Favourite flag changes in Playnite."
    },
    {
     "ask": "Set the notes on <game> to 'test note'.",
     "expect": "Notes update; nothing else changes."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Edits your real library. Change one test game and revert it.",
   "watch": [
    "gameActions replaces stored actions and must never include type LibraryPlugin."
   ]
  },
  "playnite_list_frequent": {
   "group": "playnite",
   "does": "Cached frequent games with ids and known actions.",
   "steps": [
    {
     "run": {
      "limit": 5
     },
     "expect": "Five cached games with ids/actions."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_delete_game": {
   "group": "playnite",
   "does": "Permanently deletes a game from the library.",
   "steps": [
    {
     "ask": "Delete <a throwaway game> from Playnite.",
     "expect": "Asks for confirmation before deleting."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Permanent. Only test on a game you added for this purpose."
  },
  "playnite_get_action": {
   "group": "playnite",
   "does": "Full metadata for one game action.",
   "steps": [
    {
     "run": {
      "game_id": "<id>",
      "action_id": "<action id>"
     },
     "expect": "Full action record."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_install_game": {
   "group": "playnite",
   "does": "Starts installing a game via its library.",
   "steps": [
    {
     "ask": "Install <game>.",
     "expect": "Confirms first, then the install starts."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Starts a real download/install."
  },
  "playnite_uninstall_game": {
   "group": "playnite",
   "does": "Uninstalls a game.",
   "steps": [
    {
     "ask": "Uninstall <game>.",
     "expect": "Confirms first, then uninstalls."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Removes a real installed game."
  },
  "playnite_manage_game_lists": {
   "group": "playnite",
   "does": "Sets or appends categories/tags/features/genres, or completion status.",
   "steps": [
    {
     "ask": "Add the tag 'test' to <game>.",
     "expect": "mode=add keeps existing tags."
    },
    {
     "ask": "Set <game>'s categories to just 'Test'.",
     "expect": "mode=set replaces the old list."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Edits your real library. Revert afterwards."
  },
  "playnite_fetch_game_art": {
   "group": "playnite",
   "does": "Fetches missing art for one game.",
   "steps": [
    {
     "ask": "Fetch the missing art for <game>.",
     "expect": "Cover/art appears (Steam CDN, IGDB fallback)."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json",
    "Internet"
   ]
  },
  "playnite_list_missing_art": {
   "group": "playnite",
   "does": "Lists games missing artwork.",
   "steps": [
    {
     "run": {},
     "expect": "List of games with no art."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_query_games": {
   "group": "playnite",
   "does": "Filtered library browsing.",
   "steps": [
    {
     "ask": "Which of my games are RPGs I've played for over 20 hours?",
     "expect": "Uses genres + playtimeMin filters; rows capped at 25."
    },
    {
     "ask": "Break my library down by source.",
     "expect": "Uses groupBy=source, not a game list."
    },
    {
     "run": {
      "favorite": true,
      "limit": 5
     },
     "expect": "Five favourites."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "watch": [
    "Calling it with no filters (or only installed=true) should never happen — it dumps thousands of games."
   ]
  },
  "playnite_list_collections": {
   "group": "playnite",
   "does": "Lists a Playnite collection (categories, genres, tags…).",
   "steps": [
    {
     "run": {
      "kind": "genres"
     },
     "expect": "Every genre in the library."
    },
    {
     "run": {
      "kind": "sources"
     },
     "expect": "Library sources."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_create_collection": {
   "group": "playnite",
   "does": "Creates a category/genre/tag/feature/series by name.",
   "steps": [
    {
     "run": {
      "kind": "tags",
      "name": "jarvis-test"
     },
     "expect": "New tag exists in Playnite."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Adds to your real library — remove the test tag afterwards."
  },
  "playnite_view": {
   "group": "playnite",
   "does": "Reads or controls Playnite's UI state.",
   "steps": [
    {
     "run": {
      "action": "state"
     },
     "expect": "View mode / sort state."
    },
    {
     "run": {
      "action": "selected"
     },
     "expect": "Currently selected games."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_app_info": {
   "group": "playnite",
   "does": "Playnite version, mode and paths.",
   "steps": [
    {
     "ask": "What version of Playnite am I running?",
     "expect": "Version, desktop/fullscreen mode and paths."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_list_addons": {
   "group": "playnite",
   "does": "Installed and disabled addon ids.",
   "steps": [
    {
     "run": {},
     "expect": "Two lists: installed and disabled."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_list_plugins": {
   "group": "playnite",
   "does": "Loaded, installed and disabled plugins.",
   "steps": [
    {
     "run": {},
     "expect": "Plugin lists."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_notify": {
   "group": "playnite",
   "does": "Shows a toast inside Playnite.",
   "steps": [
    {
     "run": {
      "text": "Jarvis test",
      "type": "info"
     },
     "expect": "A toast appears in Playnite."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_auto_categorize": {
   "group": "playnite",
   "does": "Auto-categorises uncategorised games by primary genre.",
   "steps": [
    {
     "ask": "Auto-categorize my uncategorized games.",
     "expect": "Games without a category receive their primary genre."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Bulk-edits the whole library. Have a backup first."
  },
  "playnite_fetch_all_art": {
   "group": "playnite",
   "does": "Fetches missing artwork for every game.",
   "steps": [
    {
     "ask": "Fetch missing art for the whole library.",
     "expect": "Runs to completion without hanging the UI."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json",
    "Internet"
   ],
   "care": "Bulk operation; can take a while."
  },
  "playnite_get_achievements": {
   "group": "playnite",
   "does": "Achievements for a game.",
   "steps": [
    {
     "ask": "Show my achievements for <game>.",
     "expect": "Achievement list."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json",
    "SuccessStory plugin"
   ]
  },
  "playnite_get_activity": {
   "group": "playnite",
   "does": "Play sessions for a game.",
   "steps": [
    {
     "ask": "Show my play sessions for <game>.",
     "expect": "Session list."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json",
    "GameActivity plugin"
   ]
  },
  "playnite_get_cover": {
   "group": "playnite",
   "does": "Downloads a game's cover/icon/background.",
   "steps": [
    {
     "run": {
      "name": "<game>",
      "type": "cover"
     },
     "expect": "File saved under ~/.jarvis/playnite-covers/ and its path returned."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ]
  },
  "playnite_eval": {
   "group": "playnite",
   "does": "Runs C# inside Playnite.",
   "steps": [
    {
     "run": {
      "code": "return PlayniteApi.Database.Games.Count;",
      "timeout_ms": 5000
     },
     "expect": "Returns the game count."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Arbitrary code inside Playnite. Only run code you understand."
  },
  "playnite_rotate_token": {
   "group": "playnite",
   "does": "Rotates the Playnite Bridge API token.",
   "steps": [
    {
     "run": {},
     "expect": "New token saved to playnite.json; the old one stops working."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Invalidates the current token everywhere it is used."
  },
  "playnite_get_skill": {
   "group": "playnite",
   "does": "Fetches the Bridge skill.md (includes the current API token).",
   "steps": [
    {
     "run": {},
     "expect": "Markdown that contains the live token."
    }
   ],
   "needs": [
    "Playnite running with the Playnite Bridge plugin",
    "Bridge token saved in ~/.jarvis/playnite.json"
   ],
   "care": "Output contains a live token — don't paste it anywhere public."
  },
  "calendar_add_event": {
   "group": "calendar",
   "does": "Adds an event to Jarvis's own calendar.",
   "steps": [
    {
     "ask": "Add 'Dentist' to my calendar tomorrow at 3pm for 45 minutes at Main Street.",
     "expect": "Event saved with the right start/duration/location and shows in calendar_events."
    },
    {
     "ask": "Note down that I have a meeting on Friday at 10.",
     "expect": "Uses the calendar (a note), not a reminder."
    }
   ],
   "watch": [
    "Subscribed feeds are read-only — this never writes to Google or Outlook."
   ]
  },
  "calendar_events": {
   "group": "calendar",
   "does": "Lists upcoming events from subscribed calendars and Jarvis's own.",
   "steps": [
    {
     "ask": "What's on my calendar today?",
     "expect": "Today's events in order."
    },
    {
     "ask": "When is my next meeting?",
     "expect": "Uses next_only; includes how long until it starts."
    },
    {
     "ask": "Am I free Thursday?",
     "expect": "Checks that day's events."
    }
   ],
   "needs": [
    "Subscribed calendar feed (for external events)"
   ]
  },
  "remind_me": {
   "group": "scheduling",
   "does": "Sets a reminder that notifies at a time.",
   "steps": [
    {
     "ask": "Remind me in 2 minutes to stretch.",
     "expect": "Notification fires after ~2 minutes even if you close the chat."
    },
    {
     "ask": "Remind me every weekday at 8:30 to check email.",
     "expect": "Recurring reminder appears in list_scheduled."
    }
   ]
  },
  "notify_me": {
   "group": "scheduling",
   "does": "Sends a notification now, or when a trigger fires.",
   "steps": [
    {
     "ask": "Notify me right now that the checklist test works.",
     "expect": "Immediate toast/notification."
    },
    {
     "ask": "Tell me when backup_done happens.",
     "expect": "Waits idle until signal_event('backup_done')."
    }
   ]
  },
  "schedule_task": {
   "group": "scheduling",
   "does": "Schedules unattended work: an ask, a command or a tool call.",
   "steps": [
    {
     "ask": "In 2 minutes, run the tool get_datetime and tell me the result.",
     "expect": "Job created; anything beyond a plain notification is pending your approval, and Jarvis says so."
    },
    {
     "ask": "Every morning at 9 summarize my calendar.",
     "expect": "Recurring 'ask' job listed in list_scheduled."
    }
   ],
   "care": "Recurring jobs keep firing — cancel the test jobs afterwards."
  },
  "schedule_watch": {
   "group": "scheduling",
   "does": "Scheduled screen check that reacts to what it sees.",
   "steps": [
    {
     "ask": "In 2 minutes check if the screen shows 'Notepad' and if so notify me, otherwise do nothing.",
     "expect": "Watch job created with ordered checks; when it runs it reads the screen text and takes at most one action."
    }
   ],
   "needs": [
    "Tesseract OCR installed (local OCR — nothing leaves the PC)"
   ],
   "care": "Cancel the watch afterwards or it keeps reading your screen."
  },
  "list_scheduled": {
   "group": "scheduling",
   "does": "Lists scheduled tasks, reminders and pending notifications.",
   "steps": [
    {
     "ask": "What do I have scheduled?",
     "expect": "Every job with next run time and id."
    },
    {
     "run": {
      "kind": "reminder",
      "include_finished": true
     },
     "expect": "Reminders only, including finished ones."
    }
   ],
   "watch": [
    "A job showing needs_approval has never run."
   ]
  },
  "cancel_scheduled": {
   "group": "scheduling",
   "does": "Cancels, pauses, resumes or snoozes one job.",
   "steps": [
    {
     "ask": "Cancel my stretch reminder.",
     "expect": "Looks up the id with list_scheduled first, then cancels."
    },
    {
     "ask": "Snooze that reminder for 10 minutes.",
     "expect": "Next run pushed back by 10 minutes."
    },
    {
     "run": {
      "id": "<job id>",
      "action": "pause"
     },
     "expect": "Job pauses and shows as paused."
    }
   ]
  },
  "signal_event": {
   "group": "scheduling",
   "does": "Announces an event, firing every job waiting on it.",
   "steps": [
    {
     "ask": "Announce that backup_done just finished.",
     "expect": "Any job waiting on 'when backup_done' fires."
    },
    {
     "run": {
      "event": "Backup done"
     },
     "expect": "Matches loosely — same event as backup_done."
    }
   ],
   "watch": [
    "Pair with a notify_me 'when backup_done' job to see it work."
   ]
  },
  "backlog_add": {
   "group": "workspace",
   "does": "Adds an untimed item to the backlog.",
   "steps": [
    {
     "ask": "Add 'try the new router idea' to my backlog.",
     "expect": "Item appears in the Backlog panel (default state todo)."
    },
    {
     "ask": "I should sort my photos at some point.",
     "expect": "Goes to the backlog, not a reminder."
    }
   ]
  },
  "backlog_list": {
   "group": "workspace",
   "does": "Lists backlog items, optionally filtered or as a board.",
   "steps": [
    {
     "ask": "What's in my backlog?",
     "expect": "Items with state/priority."
    },
    {
     "run": {
      "board": true
     },
     "expect": "Everything grouped by state."
    },
    {
     "run": {
      "state": "doing"
     },
     "expect": "Only in-progress items."
    }
   ]
  },
  "backlog_summary": {
   "group": "workspace",
   "does": "In progress, blocked, and stale items.",
   "steps": [
    {
     "ask": "What am I working on and what's blocked?",
     "expect": "Groups in-progress and blocked (with what they're blocked on) plus stale items."
    }
   ]
  },
  "backlog_update": {
   "group": "workspace",
   "does": "Changes a backlog item's state, priority, note and more.",
   "steps": [
    {
     "ask": "Mark 'try the new router idea' as doing.",
     "expect": "State changes; item matched by part of its title."
    },
    {
     "ask": "Block it on waiting for review.",
     "expect": "State=blocked with blocked_on set."
    },
    {
     "ask": "Mark it done.",
     "expect": "Moves to done."
    }
   ]
  },
  "list_daemons": {
   "group": "workspace",
   "does": "Lists every background service and whether it runs.",
   "steps": [
    {
     "ask": "What background services are running?",
     "expect": "Scheduler, Discord gateway, Instagram webhook and any user-added ones, each with running/stopped."
    },
    {
     "ask": "Register a new daemon that runs node server.js.",
     "expect": "It must NOT create one (deliberately no daemon_add) — it points you to the Daemons panel."
    },
    {
     "run": {
      "running_only": true
     },
     "expect": "Running services only."
    }
   ],
   "watch": [
    "Invariant: the model may start/stop/inspect daemons but never create one."
   ]
  },
  "daemon_status": {
   "group": "workspace",
   "does": "Detailed state of one daemon.",
   "steps": [
    {
     "ask": "What's the status of the scheduler daemon?",
     "expect": "Running flag, pid, uptime and last error."
    },
    {
     "run": {
      "id": "<daemon id>"
     },
     "expect": "Same in JSON."
    }
   ]
  },
  "daemon_start": {
   "group": "workspace",
   "does": "Starts a registered daemon.",
   "steps": [
    {
     "ask": "Start the <daemon id> service.",
     "expect": "Starts and list_daemons shows it running."
    }
   ],
   "care": "Starts a real background process."
  },
  "daemon_stop": {
   "group": "workspace",
   "does": "Stops a running daemon.",
   "steps": [
    {
     "ask": "Stop the <daemon id> service.",
     "expect": "Confirmation first, then it stops."
    }
   ],
   "care": "Stops a real service."
  },
  "daemon_restart": {
   "group": "workspace",
   "does": "Stops then starts a daemon.",
   "steps": [
    {
     "ask": "Restart the <daemon id> service.",
     "expect": "Confirmation first; pid changes after."
    }
   ],
   "care": "Restarts a real service."
  },
  "daemon_console": {
   "group": "workspace",
   "does": "Reads a daemon's recent console output.",
   "steps": [
    {
     "ask": "Why did the <daemon id> service crash?",
     "expect": "Reads recent console lines before guessing."
    },
    {
     "run": {
      "id": "<daemon id>",
      "lines": 30
     },
     "expect": "Last 30 lines."
    }
   ]
  },
  "daemon_input": {
   "group": "workspace",
   "does": "Types a line into a running daemon's console.",
   "steps": [
    {
     "run": {
      "id": "<a daemon that reads stdin>",
      "text": "hello"
     },
     "expect": "Line appears in that daemon's console."
    },
    {
     "run": {
      "id": "<a daemon that does NOT read stdin>",
      "text": "hello"
     },
     "expect": "Fails cleanly instead of silently swallowing the line."
    }
   ]
  },
  "daemon_schedule": {
   "group": "workspace",
   "does": "Sets when a daemon should next start.",
   "steps": [
    {
     "ask": "Start the <daemon id> service in 2 hours.",
     "expect": "Next-start time set."
    },
    {
     "run": {
      "id": "<daemon id>",
      "when": ""
     },
     "expect": "Empty 'when' clears the schedule."
    }
   ]
  },
  "search_log_files": {
   "group": "workspace",
   "does": "Searches raw log files on disk line by line.",
   "steps": [
    {
     "ask": "Search the logs for any traceback.",
     "expect": "Returns file, line number and matching line."
    },
    {
     "run": {
      "query": "error",
      "mode": "words",
      "sets": [
       "daemons"
      ],
      "limit": 10
     },
     "expect": "Only daemon console matches."
    }
   ],
   "watch": [
    "Different from search_conversations — this searches files, not what was said."
   ]
  },
  "list_skills": {
   "group": "skills",
   "does": "Lists every installed skill.",
   "steps": [
    {
     "ask": "What skills do I have installed?",
     "expect": "Names and descriptions; detail=true adds reference files and validity."
    },
    {
     "run": {
      "detail": true
     },
     "expect": "Extra detail for each skill."
    }
   ]
  },
  "load_skill": {
   "group": "skills",
   "does": "Loads a skill's full instructions.",
   "steps": [
    {
     "ask": "<a task that matches one of your skills>",
     "expect": "Loads the matching skill as soon as the task matches its description."
    },
    {
     "run": {
      "name": "<skill name>"
     },
     "expect": "Full instructions returned."
    }
   ],
   "needs": [
    "At least one installed skill"
   ]
  },
  "load_skill_reference": {
   "group": "skills",
   "does": "Reads one reference file of a loaded skill.",
   "steps": [
    {
     "run": {
      "name": "<skill name>",
      "file": "<a reference file>"
     },
     "expect": "That file's contents."
    }
   ],
   "needs": [
    "An installed skill that has reference files"
   ]
  },
  "create_skill": {
   "group": "skills",
   "does": "Creates a new skill from scratch.",
   "steps": [
    {
     "ask": "Create a skill that reminds me how I like commit messages written.",
     "expect": "Confirms the plan with you BEFORE writing, then the skill shows in list_skills."
    }
   ],
   "care": "Creates a real skill — remove it afterwards."
  },
  "add_skill": {
   "group": "skills",
   "does": "Installs an existing skill from a path, zip or pasted markdown.",
   "steps": [
    {
     "ask": "Add the skill at <path to a skill folder>.",
     "expect": "Installed and listed."
    },
    {
     "ask": "Add this skill: <paste SKILL.md content>.",
     "expect": "Pasted content installs when it has a description in its frontmatter."
    }
   ],
   "care": "Adds a real skill — remove it afterwards."
  },
  "remove_skill": {
   "group": "skills",
   "does": "Removes a skill and its folder.",
   "steps": [
    {
     "ask": "Remove the <test skill> skill.",
     "expect": "Confirmation first — deleting is permanent."
    }
   ],
   "care": "Permanent. Only remove a skill you created for testing."
  },
  "code_agent": {
   "group": "dev_agent",
   "does": "Self-directed read → edit → verify loop in an existing project.",
   "steps": [
    {
     "ask": "In <project path>, find out why <something> is broken and fix it.",
     "expect": "One up-front confirmation, then it explores and edits on its own; result includes a step timeline + summary."
    }
   ],
   "care": "Do this in a scratch folder or throwaway repo, not a real project.",
   "watch": [
    "Use dev_agent for new projects — code_agent is for existing ones."
   ]
  },
  "edit_file": {
   "group": "dev_agent",
   "does": "Targeted edit of an existing file.",
   "steps": [
    {
     "ask": "In <file>, change '<old text>' to '<new text>'.",
     "expect": "Confirmation, then only that text changes. It reads the file first."
    },
    {
     "run": {
      "path": "<file>",
      "old_str": "<text that appears twice>",
      "new_str": "x"
     },
     "expect": "Refused — old_str must match exactly once."
    }
   ],
   "care": "Do this in a scratch folder or throwaway repo, not a real project."
  },
  "list_dir": {
   "group": "dev_agent",
   "does": "Lists a directory two levels deep.",
   "steps": [
    {
     "run": {
      "path": "<project path>"
     },
     "expect": "Entries skip .git / node_modules / venv; top-level dotfiles are named under 'hidden'."
    }
   ]
  },
  "read_file": {
   "group": "dev_agent",
   "does": "Reads a text file as numbered lines.",
   "steps": [
    {
     "run": {
      "path": "<file>",
      "start_line": 1,
      "end_line": 20
     },
     "expect": "Lines 1–20 with numbers and total_lines."
    },
    {
     "run": {
      "path": "<a .env file>"
     },
     "expect": "Values are masked (key names only)."
    },
    {
     "run": {
      "path": "<a .env file>",
      "reveal_secrets": true
     },
     "expect": "Unmasked — only in a throwaway file."
    }
   ]
  },
  "run_shell": {
   "group": "dev_agent",
   "does": "Runs one shell command in a directory.",
   "steps": [
    {
     "ask": "In <project path>, run the tests.",
     "expect": "Confirmation + AI review note, then exit code / stdout / stderr."
    }
   ],
   "care": "Do this in a scratch folder or throwaway repo, not a real project."
  },
  "search_code": {
   "group": "dev_agent",
   "does": "Grep-style search across a directory.",
   "steps": [
    {
     "ask": "Where in <project path> is <function name> defined?",
     "expect": "file/line/snippet matches."
    },
    {
     "run": {
      "pattern": "TODO",
      "path": "<project path>",
      "max_results": 10
     },
     "expect": "At most 10 matches."
    }
   ]
  },
  "dev_agent": {
   "group": "dev_agent",
   "does": "Plans, writes, installs and self-fixes a small new project.",
   "steps": [
    {
     "ask": "Build me a tiny CLI that prints a random joke.",
     "expect": "One confirmation, then plan → write → install → run → self-fix with a step timeline."
    }
   ],
   "care": "Creates files and installs dependencies. Watch where it writes."
  },
  "remember_sender": {
   "group": "channels",
   "does": "Remembers a chat guest's name or a short detail.",
   "steps": [
    {
     "ask": "(As a guest on Discord) Call me Sam.",
     "expect": "Saved against that guest only — never into the owner's long-term memory."
    }
   ],
   "needs": [
    "Discord or Instagram configured, with a message coming from a chat guest"
   ],
   "watch": [
    "Invariant: a guest's details never go in memory.py."
   ]
  },
  "who_am_i_talking_to": {
   "group": "channels",
   "does": "Says who the current chat message is from.",
   "steps": [
    {
     "ask": "(As a guest) Who am I?",
     "expect": "Their saved name, owner or not, plus earlier notes."
    },
    {
     "ask": "(From the web console) Who am I talking to?",
     "expect": "Recognises you as the owner."
    }
   ],
   "needs": [
    "Discord or Instagram configured, with a message coming from a chat guest"
   ]
  },
  "notify_owner": {
   "group": "channels",
   "does": "Direct-messages the owner on Discord or Instagram.",
   "steps": [
    {
     "ask": "Message me on Discord when this is done.",
     "expect": "One DM reaches the configured owner. It takes no recipient — it can't message anyone else."
    },
    {
     "run": {
      "message": "checklist test",
      "platform": "any"
     },
     "expect": "Delivered to the owner account."
    }
   ],
   "needs": [
    "Discord or Instagram configured with an owner account"
   ]
  },
  "mcp_list_servers": {
   "group": "mcp",
   "does": "Lists connected MCP servers and their tool counts.",
   "steps": [
    {
     "ask": "Which MCP servers are connected?",
     "expect": "Every server, its tool count, and whether its cached tool list is stale."
    },
    {
     "run": {
      "include_tools": true
     },
     "expect": "Each server's tools are listed too."
    }
   ],
   "needs": [
    "At least one MCP server configured"
   ]
  },
  "clipboard_get": {
   "group": "clipboard",
   "does": "Reads the current text clipboard.",
   "steps": [
    {
     "ask": "What's on my clipboard?",
     "expect": "Returns the text you last copied."
    },
    {
     "run": {},
     "expect": "Returns the clipboard text; empty clipboard gives an empty string, not an error."
    }
   ],
   "watch": [
    "Jarvis shouldn't restate a copied password back verbatim in chat unless asked."
   ]
  },
  "clipboard_set": {
   "group": "clipboard",
   "does": "Puts text on the clipboard.",
   "steps": [
    {
     "ask": "Copy 'hello jarvis' to my clipboard.",
     "expect": "Confirms; pasting anywhere gives 'hello jarvis'."
    },
    {
     "run": {
      "text": "test 123"
     },
     "expect": "Clipboard now holds 'test 123'."
    }
   ],
   "care": "Overwrites whatever you currently have copied."
  },
  "clipboard_clear": {
   "group": "clipboard",
   "does": "Empties the clipboard.",
   "steps": [
    {
     "ask": "Clear my clipboard.",
     "expect": "Confirms; pasting gives nothing."
    },
    {
     "run": {},
     "expect": "clipboard_get afterwards returns empty text."
    }
   ],
   "care": "Discards whatever you currently have copied."
  },
  "clipboard_wait_for_change": {
   "group": "clipboard",
   "does": "Blocks until you copy something new, then returns it.",
   "steps": [
    {
     "ask": "Tell me when I copy something, then say what it was.",
     "expect": "Waits; after you copy new text it returns that text."
    },
    {
     "run": {
      "timeout_seconds": 5
     },
     "expect": "With no copy in 5 s it returns timed_out=true."
    }
   ],
   "watch": [
    "timeout_seconds is clamped (default ~20, max 120).",
    "Copying the same text again may not count as a change."
   ]
  },
  "browser_goto": {
   "group": "browser",
   "does": "Opens a URL in a persistent, logged-in browser session.",
   "steps": [
    {
     "ask": "Open github.com in the browser.",
     "expect": "Session starts; result gives the final URL and page title."
    },
    {
     "run": {
      "url": "file:///C:/Windows/win.ini"
     },
     "expect": "Refused — file:, javascript: and data: URLs are blocked."
    }
   ],
   "needs": [
    "Playwright and a Chromium browser installed"
   ],
   "watch": [
    "Bare URLs like 'github.com/login' get https:// added.",
    "The session persists between asks and closes when the ask ends."
   ]
  },
  "browser_get_text": {
   "group": "browser",
   "does": "Reads visible text from the open page, or from one described element.",
   "steps": [
    {
     "ask": "Open example.com and tell me what the page says.",
     "expect": "Returns the page text."
    },
    {
     "run": {
      "description": "the main heading"
     },
     "expect": "Only that element's text."
    }
   ],
   "needs": [
    "A page opened with browser_goto"
   ],
   "watch": [
    "Output is capped at 4000 characters."
   ]
  },
  "browser_click": {
   "group": "browser",
   "does": "Clicks an element described in plain English.",
   "steps": [
    {
     "ask": "On example.com, click the 'More information' link.",
     "expect": "Asks for confirmation, clicks, and reports the new page."
    },
    {
     "run": {
      "description": "a button that does not exist"
     },
     "expect": "Clear 'could not find' error, not a crash."
    }
   ],
   "needs": [
    "A page opened with browser_goto"
   ],
   "care": "Can submit forms or change settings on a real logged-in session — confirmation is required.",
   "watch": [
    "The model must describe the element, never supply a CSS selector."
   ]
  },
  "browser_fill": {
   "group": "browser",
   "does": "Types text into a field described in plain English.",
   "steps": [
    {
     "ask": "On a search page, type 'jarvis' into the search box.",
     "expect": "Asks for confirmation, then the field contains 'jarvis'."
    },
    {
     "run": {
      "description": "the search field",
      "text": "hello"
     },
     "expect": "Field filled with 'hello'."
    }
   ],
   "needs": [
    "A page opened with browser_goto"
   ],
   "care": "Writes into a real, possibly logged-in session — confirmation is required."
  },
  "browser_wait_for": {
   "group": "browser",
   "does": "Waits for an element to appear on the page.",
   "steps": [
    {
     "run": {
      "description": "the main heading",
      "timeout_seconds": 5
     },
     "expect": "Returns once the element is visible."
    },
    {
     "run": {
      "description": "a banner that never appears",
      "timeout_seconds": 3
     },
     "expect": "Times out after ~3 s with a clear message."
    }
   ],
   "needs": [
    "A page opened with browser_goto"
   ],
   "watch": [
    "timeout_seconds defaults to 10 and is clamped to 120.",
    "description is required."
   ]
  },
  "browser_screenshot": {
   "group": "browser",
   "does": "Screenshots the open page and shows it in the Jarvis UI.",
   "steps": [
    {
     "ask": "Take a screenshot of the page.",
     "expect": "The image appears in the UI; the model itself doesn't receive it."
    },
    {
     "run": {},
     "expect": "Result reports success and the image shows up in the console."
    }
   ],
   "needs": [
    "A page opened with browser_goto"
   ]
  },
  "browser_close": {
   "group": "browser",
   "does": "Closes the browser session early.",
   "steps": [
    {
     "ask": "Close the browser.",
     "expect": "Session closes; the next browser_goto starts a fresh one."
    },
    {
     "run": {},
     "expect": "Succeeds quietly even if no browser is open."
    }
   ]
  },
  "make_dir": {
   "group": "files",
   "does": "Creates a folder (and missing parents).",
   "steps": [
    {
     "ask": "Make a folder called jarvis-test on my Desktop.",
     "expect": "Asks for confirmation, then the folder exists."
    },
    {
     "run": {
      "path": "<existing folder>"
     },
     "expect": "Succeeds quietly — already existing is not an error."
    }
   ],
   "care": "Creates real folders — use a scratch location."
  },
  "copy_path": {
   "group": "files",
   "does": "Copies a file or folder, leaving the original.",
   "steps": [
    {
     "ask": "Copy jarvis-test.txt on my Desktop into the jarvis-test folder.",
     "expect": "Asks for confirmation; both original and copy exist."
    },
    {
     "run": {
      "src": "<file>",
      "dest": "<existing file>"
     },
     "expect": "Refuses to overwrite without overwrite=true."
    }
   ],
   "care": "Writes real files — use scratch files.",
   "watch": [
    "Folders are never overwritten.",
    "dest may be an existing folder (keeps its name) or a full new path."
   ]
  },
  "move_path": {
   "group": "files",
   "does": "Moves a file or folder to another folder or new path.",
   "steps": [
    {
     "ask": "Move jarvis-test.txt on my Desktop into the jarvis-test folder.",
     "expect": "Asks for confirmation; file is only in the new location."
    },
    {
     "run": {
      "src": "<file>",
      "dest": "<existing file>"
     },
     "expect": "Refuses to overwrite without overwrite=true."
    }
   ],
   "care": "Moves real files — use scratch files.",
   "watch": [
    "Uses src/dest argument names.",
    "Result is verified after the move."
   ]
  },
  "rename_path": {
   "group": "files",
   "does": "Renames a file or folder in place.",
   "steps": [
    {
     "ask": "Rename jarvis-test.txt on my Desktop to jarvis-final.txt.",
     "expect": "Asks for confirmation; only the name changes."
    },
    {
     "run": {
      "path": "<file>",
      "new_name": "sub/x.txt"
     },
     "expect": "Rejected — new_name must be a bare name with no folder part."
    }
   ],
   "care": "Renames real files — use a scratch file.",
   "watch": [
    "Never overwrites an existing name."
   ]
  },
  "delete_path": {
   "group": "files",
   "does": "Sends a file or folder to the Recycle Bin/Trash.",
   "steps": [
    {
     "ask": "Delete jarvis-final.txt from my Desktop.",
     "expect": "Asks for confirmation; the file lands in the Recycle Bin and can be restored."
    },
    {
     "run": {
      "path": "<nonexistent path>"
     },
     "expect": "Clear 'not found' error."
    }
   ],
   "care": "Real deletion (recoverable via the Recycle Bin) — use a scratch file.",
   "watch": [
    "There is no permanent-delete option."
   ]
  }
 }
} /*JSON-END*/;
