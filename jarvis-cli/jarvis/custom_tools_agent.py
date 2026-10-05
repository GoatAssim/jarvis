"""Ask Jarvis -- a chat that writes the tool WITH you, inside the Tool Manager
editor (master plan L.43).

The owner types what they want ("a tool that lists my 10 biggest downloads"),
this module asks one provider to write the whole file, and the tokens stream
into the open editor tab as they are produced, so the owner watches the code
being written and can stop it at any time.

THE BOUNDARY (AGENTS.md: the model can't create or change custom tools)
-----------------------------------------------------------------------
This module only ever returns TEXT. It never calls ctools-write, never touches
~/.jarvis/tools, and gives the model no tools at all. What streams into the
editor is an unsaved buffer: the owner still presses Save, and Save still runs
custom_tools_store.validate_source. A reply the owner doesn't like is undone
with one click (the browser keeps the pre-edit text), and Ctrl+Z works too.

REPLY SHAPE
-----------
A short note (1-3 sentences), then ONE ```python fence holding the COMPLETE new
file. A reply with no fence is a question or a refusal: the editor is left
untouched and the note is shown in the chat. Whole-file replies (not diffs)
keep the protocol trivial and the result always a loadable file.

WIRE FORMAT (stdout, one line each; everything else on stdout is ignored)
    JARVIS_STREAM {"k":"text","d":"..."}      token deltas (stream_markers.py)
    JARVIS_STREAM {"k":"reset"}               a key failed mid-reply; start over
    JARVIS_AGENT_DONE {"ok":true,"note":...,"code":...,"provider":...}
    JARVIS_AGENT_DONE {"ok":false,"error":...}
The DONE line is authoritative: a provider that can't stream still works, the
browser just receives everything at once.
"""

from __future__ import annotations

import json
import re
import sys

from . import ai_config
from . import ai_providers
from .custom_tools_suggest import _pick_provider

AGENT_TIMEOUT = 150
AGENT_MAX_TOKENS = 8000          # a FLOOR: a larger max_tokens on the provider block wins
# A whole-file reply that hits the provider's output cap is continued in a fresh
# call instead of thrown away. Each continuation is one more full call, so it is
# bounded; a tool that still doesn't fit after this many is genuinely too big.
MAX_CONTINUATIONS = 3
_OVERLAP_WINDOW = 400
_CONTINUE_PROMPT = (
    "Your reply was cut off by the output limit. Continue the file from the exact "
    "character where you stopped. Output ONLY the remaining code followed by the "
    "closing ``` fence: no new note, no new opening fence, and do not repeat any "
    "line you already wrote."
)
MAX_SOURCE_CHARS = 120_000       # custom_tools_store.MAX_SOURCE_CHARS
MAX_INSTRUCTION_CHARS = 4000
MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS = 600
DONE_MARKER = "JARVIS_AGENT_DONE "

_FENCE_OPEN = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n", re.IGNORECASE)

_SYSTEM_PROMPT = (
    "You are Jarvis, helping the owner write a custom tool for yourself in the "
    "Tool Manager's code editor. The file is ONE Python module that adds tools "
    "to Jarvis. Contract: handler functions take a dict `args` and return a "
    "JSON-able dict that includes \"ok\" (True/False, plus \"error\" text when "
    "False); TOOL_SCHEMAS is a list of {name, description, parameters} dicts "
    "(parameters is a JSON-schema object); TOOLS maps each schema name to its "
    "handler; TOOL_GROUP is a string; TOOL_KEYWORDS (optional) maps a tool name "
    "to {phrase: weight} so it gets routed; TEST_CHECKLIST (optional) says how "
    "to test each tool. Tool names are lower_snake_case. Do NOT import "
    "ai_client, tool_router or tool_registry at module level (circular import); "
    "import inside the handler if you must. Prefer the standard library. Never "
    "delete, overwrite or send anything off the machine unless the owner asked "
    "for exactly that, and say so in your note when a tool does. Keep the file "
    "compact -- the whole module is re-sent on every turn and replies have a hard "
    "output limit: short comments, no redundant helpers, at most two test steps "
    "per tool.\n\n"
    "HOW TO ANSWER. Write a short note first (1-3 plain sentences: what you are "
    "doing or changing). Then give the COMPLETE updated file in a single "
    "```python fence -- the whole module every time, never a fragment or a "
    "diff, because it replaces the editor's text. Keep every part of the "
    "existing file that still works and the owner's style. After the closing "
    "fence you may add one short sentence. If the request is unclear or you "
    "need a decision first, ask ONE question in plain text and include NO code "
    "fence; the editor is then left alone."
)


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def build_messages(instruction, source, name="draft", history=None):
    """System prompt, the last few chat turns (notes only, never code), then the
    request with the file as it is RIGHT NOW -- the owner may have typed in the
    editor since the last turn, and that text is the truth."""
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = _clip(turn.get("content"), MAX_HISTORY_CHARS).strip()
        if content:
            messages.append({"role": role, "content": content})
    body = str(source or "").strip()
    current = ("The editor is empty." if not body
               else "Current contents of %s.py:\n```python\n%s\n```" % (name, source))
    messages.append({"role": "user", "content": "%s\n\nRequest: %s" % (
        current, _clip(instruction, MAX_INSTRUCTION_CHARS).strip())})
    return messages


def split_reply(text):
    """(note, code, complete). `code` is "" when the reply has no fence (a
    question or refusal). `complete` is False while the fence is still open --
    the browser uses the same rule to type into the editor live."""
    t = str(text or "").replace("\r\n", "\n")
    m = _FENCE_OPEN.search(t)
    if not m:
        return t.strip(), "", False
    note = t[:m.start()].strip()
    rest = t[m.end():]
    close = rest.find("\n```")
    if close == -1 and rest.rstrip().endswith("```"):
        close = len(rest.rstrip()) - 3
        body, complete, after = rest[:close], True, ""
    elif close == -1:
        body, complete, after = rest, False, ""
    else:
        body, complete, after = rest[:close], True, rest[close + 4:].strip()
    code = body.strip("\n")
    if code:
        code += "\n"
    if after and complete:
        note = (note + "\n\n" + after).strip() if note else after
    return note, code, complete


def _done(payload):
    try:
        sys.stdout.write(DONE_MARKER + json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 -- the pipe going away is not our error
        pass


def _output_cap(provider):
    """The provider's own max_tokens if it set a bigger one than our floor.
    Overwriting it with a flat 8000 meant raising max_tokens in the AI config
    did nothing for this feature."""
    try:
        configured = int(provider.get("max_tokens") or 0)
    except (TypeError, ValueError):
        configured = 0
    return max(configured, AGENT_MAX_TOKENS)


def _join_continuation(prev, more):
    """Append a continuation to the text so far. A model told to continue
    sometimes re-opens a fence or repeats its last few characters; drop both."""
    more = str(more or "").replace("\r\n", "\n")
    m = _FENCE_OPEN.match(more.lstrip())
    if m:
        more = more.lstrip()[m.end():]
    for k in range(min(len(prev), len(more), _OVERLAP_WINDOW), 7, -1):
        if prev.endswith(more[:k]):
            more = more[k:]
            break
    return prev + more


def _call_once(adapter, attempt, messages, label, out):
    """One adapter call with streaming wired to `out`. (result, error_text)."""
    from .stream_markers import StreamMarkerSink
    sink = StreamMarkerSink(out=out)
    ai_providers.set_log_context(None, provider=label, stream=True)
    ai_providers.set_stream_sink(sink)
    try:
        return adapter(attempt, messages, attempt["timeout"],
                       tools=None, tool_executor=None), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]
    finally:
        sink.close()
        ai_providers.clear_log_context()


def _agent_provider(cfg):
    """A capable model, not the cheap completion one: writing a whole tool is
    the opposite of ghost text. First keyed provider that is not a
    completion/summarizer helper; else whatever suggestions would use."""
    providers = [p for p in (cfg.get("providers") or [])
                 if isinstance(p, dict) and p.get("enabled", True)
                 and (p.get("type") == "ollama" or ai_config.provider_keys(p))]
    for p in providers:
        if not p.get("completion") and not p.get("summarizer"):
            return p
    return _pick_provider(cfg)


def run(payload, cfg=None, out=None):
    """Run one agent turn, streaming to `out`'s stdout markers. Never raises;
    always ends with exactly one DONE line."""
    payload = payload if isinstance(payload, dict) else {}
    source = payload.get("source")
    source = source if isinstance(source, str) else ""
    instruction = str(payload.get("instruction") or "").strip()
    name = str(payload.get("name") or "draft")
    if not instruction:
        return _done({"ok": False, "error": "say what you want the tool to do"})
    if len(source) > MAX_SOURCE_CHARS:
        return _done({"ok": False, "error": "the file is too large to work on here"})
    try:
        cfg = cfg if cfg is not None else ai_config.load_ai_config()
    except Exception as exc:  # noqa: BLE001
        return _done({"ok": False, "error": "couldn't read the AI config: %s" % exc})
    provider = _agent_provider(cfg)
    if not provider:
        return _done({"ok": False, "error": "no AI provider with a key is configured"})
    adapter = ai_providers.ADAPTERS.get(provider.get("type"))
    if adapter is None:
        return _done({"ok": False, "error": "unsupported provider type"})

    messages = build_messages(instruction, source, name, payload.get("history"))
    resolved = dict(provider)
    resolved["timeout"] = max(resolved.get("timeout") or 0, AGENT_TIMEOUT)
    resolved["max_tokens"] = _output_cap(provider)
    label = str(provider.get("name") or provider.get("type") or "")

    last_error = "the provider didn't answer"
    keys = ai_config.provider_keys(provider) or [None]
    for i, key_val in enumerate(keys):
        attempt = dict(resolved)
        if key_val is not None:
            attempt["api_key"] = key_val
        result, err = _call_once(adapter, attempt, messages, label, out)
        if err:
            last_error = err or last_error
        if result is not None and getattr(result, "ok", False):
            text = getattr(result, "text", "") or ""
            cut = getattr(result, "cut", None)
            note, code, complete = split_reply(text)
            # The provider said it stopped on its output limit with the file still
            # open: ask for the rest instead of discarding everything written.
            extra = 0
            while (code and not complete and cut == ai_providers.CUT_LENGTH
                   and extra < MAX_CONTINUATIONS):
                extra += 1
                more, _err = _call_once(
                    adapter, attempt,
                    messages + [{"role": "assistant", "content": text},
                                {"role": "user", "content": _CONTINUE_PROMPT}],
                    label, out)
                if more is None or not getattr(more, "ok", False):
                    break
                text = _join_continuation(text, getattr(more, "text", "") or "")
                cut = getattr(more, "cut", None)
                note, code, complete = split_reply(text)
            if code and not complete:
                if cut == ai_providers.CUT_LENGTH:
                    why = ("(The reply was cut off at the output limit (max_tokens %d) "
                           "before the file ended -- not applied. Raise max_tokens on "
                           "the %s provider, or ask for a smaller tool.)"
                           % (attempt.get("max_tokens") or 0, label or "AI"))
                else:
                    why = "(The reply was cut off before the file ended -- not applied.)"
                note = (note + "\n\n" if note else "") + why
                code = ""
            if len(code) > MAX_SOURCE_CHARS:
                note, code = "The file came out too large to apply.", ""
            return _done({"ok": True, "note": note, "code": code, "provider": label})
        err = getattr(result, "error", None)
        if err:
            last_error = str(err)[:200]
        if i + 1 < len(keys):
            # Tell the browser to throw away the partial text before the next key writes.
            try:
                (out or sys.stdout).write('JARVIS_STREAM {"k":"reset"}\n')
                (out or sys.stdout).flush()
            except Exception:  # noqa: BLE001
                pass
    return _done({"ok": False, "error": last_error})
