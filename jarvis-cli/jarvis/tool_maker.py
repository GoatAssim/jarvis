"""The Tool Maker conversation (master plan L.53).

In the Tool Manager's side panel the owner talks to Jarvis the way they do in
the main Ask panel -- same ask path, same providers, same tools, a saved
conversation -- except that every turn carries the Tool Maker's context (the
tool's name and the whole file as it is RIGHT NOW, unsaved edits included) and
the conversation may only EDIT the editor's text.

THE BOUNDARY (AGENTS.md: "The Tool Maker conversation may only edit")
--------------------------------------------------------------------
Validate executes a file's top level, and Save loads it, so a model allowed to
save and validate would run its own code unattended. The owner decided
(2026-10-09g) that this conversation edits the editor's text and nothing else:
Validate, Save and running a tool stay the owner's. None of ctools-write /
ctools-check / ctools-run is a model tool, so the real paths a model could use
to save or run a file are the GENERIC ones: write a file, run a shell command,
run a saved command, drive git, or hand the job to a code / dev / sub agent.
Those are WITHHELD_TOOLS below. They are withheld BY NAME, in code, not by
prompt wording (the prompt only explains why).

HOW IT IS SWITCHED ON
---------------------
web/server.js sets JARVIS_TOOL_MAKER_CONTEXT_FILE on the `jarvis ask` it
spawns for the panel, pointing at a private temp file holding
{"name": ..., "source": ...} (a file, not an env value or an argv element,
because the whole module can be 120 000 characters and Windows caps the
environment and the command line far below that). The variable can only ever
ADD restrictions: the withheld set applies whenever it is set, even when the
file itself is missing or unreadable (fail closed -- an unreadable context
means "no context", never "no restrictions").

Nothing here touches ~/.jarvis/tools, and nothing here is reachable by the
model.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ORIGIN = "tool-maker"                       # conversations.new_conversation(origin=...)
CONTEXT_ENV = "JARVIS_TOOL_MAKER_CONTEXT_FILE"
CONTEXT_PREFIX = "jarvis-tm-ctx-"           # basename prefix server.js gives the temp file
MAX_SOURCE_CHARS = 120_000                  # custom_tools_store.MAX_SOURCE_CHARS
MAX_CONTEXT_BYTES = 4 * MAX_SOURCE_CHARS    # a 120 000-char file is at most 4 bytes per char in UTF-8
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
UNTITLED = "new tool"                       # what an unsaved, unnamed tab is called

# Tools a model could use to SAVE or RUN a custom tool (write a file anywhere,
# run a program, run a saved command, drive git, delegate to an agent that can
# do any of those). Withheld in this conversation only. A tool the owner already
# saved and loaded is NOT in this set: running it is the owner's own earlier
# decision, not AI-written code running unattended.
WITHHELD_TOOLS = frozenset({
    "write_file", "edit_file",
    "run_shell", "run_command", "run_custom_command", "run_chain",
    "create_command", "update_command",
    "git_run", "git_commit_all",
    "code_agent", "dev_agent", "spawn_subagent", "run_subagents",
})

_STORED_CODE_FENCE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)(?:\n[ \t]*```|\Z)", re.DOTALL | re.IGNORECASE)
_COLLAPSE_OVER = 400                        # chars; shorter snippets are conversation, not the file


def active():
    """True when this process is a Tool Maker turn. Only the SET-ness of the
    variable counts, so a missing or corrupt context file cannot lift the
    restrictions."""
    return bool(os.environ.get(CONTEXT_ENV, "").strip())


def withheld_tools():
    """The tool names that must not be offered or run right now."""
    return WITHHELD_TOOLS if active() else frozenset()


def refusal_for(name):
    """What a withheld tool's caller is told. Plain, and names the buttons."""
    return {
        "ok": False,
        "error": ("%s is not available in the Tool Maker panel: this conversation may only edit the "
                  "text in the editor. Writing files and running programs is how a tool would get "
                  "saved or run, and that stays yours. Tell the owner to press Validate to check "
                  "the file or Save to keep it (the buttons above the code)." % name),
        "retryable": False,
    }


def _context_file():
    raw = os.environ.get(CONTEXT_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    if not path.name.startswith(CONTEXT_PREFIX) or path.suffix != ".json":
        return None
    return path


def clean_name(name):
    name = str(name or "").strip().lower()
    return name if _NAME_RE.match(name) else ""


def load_context():
    """{"name", "source"} for this turn, or None (no variable, or a file that is
    missing, oversize, not JSON or the wrong shape). Never raises."""
    path = _context_file()
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_CONTEXT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    source = data.get("source")
    source = source if isinstance(source, str) else ""
    if len(source) > MAX_SOURCE_CHARS:
        return None
    return {"name": clean_name(data.get("name")), "source": source}


_RULES = (
    "TOOL MAKER. The owner is in the Tool Manager's code editor and is talking to you in its side panel. "
    "They are writing ONE Python module that adds tools to you. Contract: handler functions take a dict "
    "`args` and return a JSON-able dict that includes \"ok\" (True/False, plus \"error\" text when False); "
    "TOOL_SCHEMAS is a list of {name, description, parameters} dicts (parameters is a JSON-schema object); "
    "TOOLS maps each schema name to its handler; TOOL_GROUP is a string; TOOL_KEYWORDS (optional) maps a "
    "tool name to {phrase: weight} so it gets routed; TEST_CHECKLIST (optional) says how to test each tool. "
    "Tool names are lower_snake_case. Do NOT import ai_client, tool_router or tool_registry at module level "
    "(circular import); import inside the handler if you must. Prefer the standard library. Never delete, "
    "overwrite or send anything off the machine unless the owner asked for exactly that, and say so in your "
    "note when a tool does. Keep the file compact: the whole module is re-sent every turn and replies have a "
    "hard output limit (short comments, no redundant helpers, at most two test steps per tool).\n\n"
    "HOW TO ANSWER. To change the file: a short note first (1-3 plain sentences: what you are doing or "
    "changing), then the COMPLETE updated file in a single ```python fence -- the whole module every time, "
    "never a fragment or a diff, because it replaces the editor's text and is typed into the editor while the "
    "owner watches. Keep every part of the existing file that still works and the owner's style. After the "
    "closing fence you may add one short sentence. If the request is unclear, or is only a question, or you need "
    "a decision first, answer in plain text with NO code fence; the editor is then left alone. You may use "
    "your other tools to look something up, but write no fence until you are ready to give the final file.\n\n"
    "YOUR LIMITS. You may only EDIT the editor's text. You cannot save the file, validate it or run it, and you "
    "have no tool that writes files or runs programs here. If asked to save, validate or run, say in ONE line "
    "that you only edit, and that the owner presses Validate (to check it) or Save (to keep it) above the code; "
    "do not attempt it. Validate runs the file's top level, so remind the owner to read your change first when "
    "you have rewritten a lot."
)


def prompt_context(ctx):
    """The per-turn block for the TAIL of the system prompt (never the cached
    static prefix: it differs on every turn). `ctx` is load_context()'s result,
    or None when the file could not be read."""
    if ctx is None:
        return (_RULES + "\n\nThe editor's current contents could not be read this turn. Say so, and ask the "
                "owner to paste the part that matters rather than guessing.")
    name = ctx.get("name") or ""
    source = ctx.get("source") or ""
    label = ("%s.py" % name) if name else "an unsaved new tool"
    if not source.strip():
        current = "The editor is empty (%s)." % label
    else:
        current = "Current contents of %s (including unsaved edits):\n```python\n%s\n```" % (label, source)
    return "%s\n\n%s" % (_RULES, current)


def collapse_code_for_history(messages):
    """The same conversation, with every long ```python block in an ASSISTANT
    turn replaced by a one-line marker. The whole file rides along in each
    turn's own context, so an old copy in the history is stale, doubles the
    tokens and eats the history budget that should hold the actual
    conversation. The saved conversation and the raw log keep the full text;
    only what is sent back to the model changes. Returns a new list."""
    out = []
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str):
            def _sub(match):
                body = match.group(1)
                if len(body) < _COLLAPSE_OVER:
                    return match.group(0)
                lines = body.count("\n") + 1
                return ("[a %d-line version of the file was typed into the editor here; "
                        "its current text is given above]" % lines)
            text = _STORED_CODE_FENCE.sub(_sub, m["content"])
            if text != m["content"]:
                m = dict(m, content=text)
        out.append(m)
    return out


def stamp_conversation(conv_id):
    """Keep the conversation's origin tag and detail (the tool's name) in step
    with the editor: a new-tool tab that was saved under a name since the last
    turn is now that tool. Never raises, never overwrites a different origin."""
    try:
        if not active() or not conv_id:
            return False
        ctx = load_context()
        detail = (ctx or {}).get("name") or UNTITLED
        from . import conversations
        return conversations.set_origin(conv_id, ORIGIN, detail)
    except Exception:  # noqa: BLE001 -- a tag must never break an ask
        return False
