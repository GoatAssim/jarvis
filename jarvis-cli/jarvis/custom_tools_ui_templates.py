"""The two Tool Manager templates that give a tool a screen of its own (TOOL_UI).

Kept apart from custom_tools_store.TEMPLATES for one reason: each of these is three
files plus a Python file, and the web/JS text inside a Python string literal is
miserable to read and edit when it shares a file with a dozen other templates.
custom_tools_store merges UI_TEMPLATES into TEMPLATES, so to every caller they are
templates like the rest; the only difference is the two extra keys:

    "folder"  the folder (relative to ~/.jarvis/tools) the starter files go in.
              Saving a tool created from the template also writes it -- once, and
              never over a folder that already exists (scaffold_template).
    "files"   {relative file name: text}

Both are written as RAW strings (r'''...''') so that a backslash in the JS reaches
the file exactly as typed.

The `host` object the JS talks to is defined in web/public/tool-ui.js; the contract
and the two modes are documented in jarvis/tool_ui.py and actions/_template.py
section 9. These starters stay deliberately small: one tool, one page, the whole
loop (page -> host.runTool -> tool -> page) so there is a working thing to change.
"""

_BUTTON_PY = r'''"""A tool with a page of its own, opened from a BUTTON beside the Menu button."""

import random


def tool_roll_dice(args):
    args = args or {}
    try:
        sides = int(args.get("sides") or 6)
    except (TypeError, ValueError):
        return {"ok": False, "error": "sides must be a whole number"}
    if sides < 2 or sides > 1000:
        return {"ok": False, "error": "sides must be between 2 and 1000"}
    return {"ok": True, "sides": sides, "roll": random.randint(1, sides)}


TOOL_SCHEMAS = [
    {
        "name": "roll_dice",
        "description": "Roll one die with a given number of sides. Use when the "
                       "user asks to roll a die or pick a random number.",
        "parameters": {
            "type": "object",
            "properties": {
                "sides": {"type": "integer", "description": "Sides on the die (2-1000). Defaults to 6."},
            },
            "required": [],
        },
    },
]

TOOLS = {"roll_dice": tool_roll_dice}
TOOL_GROUP = "custom"
TOOL_KEYWORDS = {"roll_dice": {"roll a die": 10, "roll dice": 10, "dice": 8}}

# THE PAGE. A "button" is a button beside the Menu button; clicking it opens
# dice_page/tool.html in a window of its own. The page can't touch the rest of
# Jarvis -- it talks through the `host` object (see dice_page/tool.js).
#
#   id     unique across Jarvis, lower_snake_case
#   path   the folder with your files, relative to THIS file (required)
#   html / js / css   default to tool.html / tool.js / tool.css in that folder;
#                     name another file, or "" for none
#   tool   the tool above this page belongs to (optional)
#
# Prefer a Menu entry that opens in a panel instead? Use mode "menu" (see the
# "Menu panel" template). The older way -- a tool popping a toast or a question
# while it runs -- is still there too: the "Shows a popup" template.
TOOL_UI = [
    {
        "id": "dice_page",
        "label": "Dice",
        "icon": "D6",
        "mode": "button",
        "path": "dice_page",
        "title": "Dice roller",
        "tool": "roll_dice",
    },
]

# What Menu > Test Checklist shows for the tool (see the minimal template for the
# field-by-field notes). The "watch" lines are for the page itself.
TEST_CHECKLIST = {
    "roll_dice": {
        "does": "Rolls a die; also has a page (the Dice button) that does it with a click.",
        "steps": [
            {"ask": "Roll a 20-sided die.",
             "expect": "Replies with a number from 1 to 20."},
            {"run": {"sides": 6},
             "expect": "ok: true, sides 6 and a roll between 1 and 6."},
            {"run": {"sides": 1},
             "expect": "ok: false with 'sides must be between 2 and 1000'."},
        ],
        "watch": [
            "Click the Dice button beside Menu: a window opens, Roll shows a number.",
            "Switch the dice_page element off in Tool Manager: the button disappears.",
        ],
    },
}
'''

_BUTTON_HTML = r'''<div class="dice">
  <h2>Dice roller</h2>
  <label>Sides <input id="sides" type="number" min="2" max="1000" value="6"></label>
  <button id="roll" type="button">Roll</button>
  <p id="result" aria-live="polite">Press Roll.</p>
</div>
'''

_BUTTON_JS = r'''// `host` is provided by Jarvis. host.root is what to query (the page's document here).
// Other things on host: runTool(name, args), toast(message, level), setTitle(text),
// close(), storage.get(key) / storage.set(key, value), onClose(fn), id, mode, tools.
const root = host.root;
const sides = root.querySelector("#sides");
const result = root.querySelector("#result");

root.querySelector("#roll").addEventListener("click", async () => {
  result.textContent = "Rolling...";
  try {
    // Runs the roll_dice tool from tool.py. If the tool is set to ask first,
    // Jarvis shows the same confirmation it would for the model.
    const r = await host.runTool("roll_dice", { sides: Number(sides.value) });
    result.textContent = r.ok ? "You rolled " + r.roll + " (d" + r.sides + ")" : (r.error || "That didn't work.");
  } catch (err) {
    result.textContent = err.message;
  }
});
'''

_BUTTON_CSS = r'''/* Colours arrive as variables (--accent, --bg, --bg-panel, --border, --text), the
   same ones Jarvis uses, so the page follows the current skin. */
body { font-family: system-ui, sans-serif; margin: 0; padding: 16px; background: var(--bg-panel, #111); color: var(--text, #ddd); }
h2 { margin: 0 0 12px; font-size: 16px; color: var(--accent, #7dd3fc); }
label { display: inline-flex; gap: 8px; align-items: center; }
input { width: 80px; padding: 4px 6px; background: var(--bg, #000); color: inherit; border: 1px solid var(--border, #444); border-radius: 4px; }
button { margin-left: 8px; padding: 5px 14px; cursor: pointer; background: transparent; color: var(--accent, #7dd3fc); border: 1px solid var(--accent, #7dd3fc); border-radius: 4px; }
button:hover { background: var(--accent, #7dd3fc); color: var(--bg, #000); }
#result { margin-top: 14px; font-size: 18px; }
'''

_MENU_PY = r'''"""A tool with a panel of its own, opened from an entry in the MENU."""


def tool_count_words(args):
    args = args or {}
    text = args.get("text")
    if not isinstance(text, str):
        return {"ok": False, "error": "text must be a string"}
    return {
        "ok": True,
        "words": len(text.split()),
        "characters": len(text),
        "lines": len(text.splitlines()),
    }


TOOL_SCHEMAS = [
    {
        "name": "count_words",
        "description": "Count the words, characters and lines in a piece of text. "
                       "Use when the user asks how long a text is.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to count."},
            },
            "required": ["text"],
        },
    },
]

TOOLS = {"count_words": tool_count_words}
TOOL_GROUP = "custom"
TOOL_KEYWORDS = {"count_words": {"word count": 10, "count words": 10, "how many words": 10}}

# THE PANEL. A "menu" element is an entry in the Menu (like Test Checklist);
# clicking it opens a panel and your word_count_panel/tool.html goes into a div in
# it, with tool.css scoped to that div and tool.js running against it. Everything
# else about TOOL_UI is in the "Button page" template and in tool_ui.py.
TOOL_UI = [
    {
        "id": "word_count_panel",
        "label": "Word counter",
        "mode": "menu",
        "path": "word_count_panel",
        "title": "Word counter",
        "hint": "Count the words in some text",
        "tool": "count_words",
    },
]

TEST_CHECKLIST = {
    "count_words": {
        "does": "Counts words, characters and lines; also has a Menu panel that does it as you type.",
        "steps": [
            {"ask": "How many words are in: the quick brown fox?",
             "expect": "Says 4 words."},
            {"run": {"text": "one two\nthree"},
             "expect": "ok: true, words 3, lines 2."},
            {"run": {"text": 5},
             "expect": "ok: false with 'text must be a string'."},
        ],
        "watch": [
            "Menu > Word counter opens a panel; typing updates the counts.",
            "Switch the word_count_panel element off in Tool Manager: the Menu entry disappears.",
        ],
    },
}
'''

_MENU_HTML = r'''<div class="wc">
  <textarea id="text" rows="8" placeholder="Type or paste some text..."></textarea>
  <div class="wc__counts">
    <span>Words <b id="words">0</b></span>
    <span>Characters <b id="chars">0</b></span>
    <span>Lines <b id="lines">0</b></span>
  </div>
</div>
'''

_MENU_JS = r'''// `host` is provided by Jarvis. In a Menu panel host.root is a shadow root: query it,
// don't use document (the rest of Jarvis is outside it, on purpose).
// Other things on host: runTool(name, args), toast(message, level), setTitle(text),
// close(), storage.get(key) / storage.set(key, value), onClose(fn), id, mode, tools.
const root = host.root;
const text = root.querySelector("#text");
let timer = 0;

async function recount() {
  try {
    const r = await host.runTool("count_words", { text: text.value });
    if (!r.ok) throw new Error(r.error || "count failed");
    root.querySelector("#words").textContent = r.words;
    root.querySelector("#chars").textContent = r.characters;
    root.querySelector("#lines").textContent = r.lines;
  } catch (err) {
    host.toast(err.message, "error");
  }
}

text.addEventListener("input", () => {
  clearTimeout(timer);
  timer = setTimeout(recount, 250);
  host.storage.set("draft", text.value).catch(() => {});
});

// Remember what was typed between openings.
host.storage.get("draft").then((saved) => { if (saved) { text.value = saved; recount(); } }).catch(() => {});
host.onClose(() => clearTimeout(timer));
'''

_MENU_CSS = r'''/* This stylesheet only applies inside the panel. Jarvis's variables (--accent,
   --bg, --bg-panel, --border, --text) pass straight through. */
:host { display: block; }
.wc { display: flex; flex-direction: column; gap: 12px; padding: 16px; color: var(--text, #ddd); }
textarea { width: 100%; box-sizing: border-box; padding: 8px; background: var(--bg, #000); color: inherit; border: 1px solid var(--border, #444); border-radius: 4px; font: inherit; resize: vertical; }
.wc__counts { display: flex; gap: 20px; }
.wc__counts b { color: var(--accent, #7dd3fc); margin-left: 4px; }
'''

UI_TEMPLATES = {
    "ui_button": {
        "label": "Button page",
        "hint": "A button beside Menu that opens a page built from your own html/js/css.",
        "source": _BUTTON_PY,
        "folder": "dice_page",
        "files": {"tool.html": _BUTTON_HTML, "tool.js": _BUTTON_JS, "tool.css": _BUTTON_CSS},
    },
    "ui_menu": {
        "label": "Menu panel",
        "hint": "A Menu entry that opens a panel built from your own html/js/css.",
        "source": _MENU_PY,
        "folder": "word_count_panel",
        "files": {"tool.html": _MENU_HTML, "tool.js": _MENU_JS, "tool.css": _MENU_CSS},
    },
}
