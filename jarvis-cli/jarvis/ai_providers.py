"""One small adapter per AI provider "type". Each adapter takes a resolved
provider config + a generic messages list, and returns an AIResult.

Generic message shape in (what ai_client.py builds):
    [{"role": "system"|"user"|"assistant", "content": "..."}, ...]

Every adapter translates that into whatever shape its provider actually
wants, and translates the response back into plain text — or a short,
human-readable reason it didn't get one. ai_client.py's failover loop
doesn't care *why* a provider failed, only that it did, so the reasons
here are for the trace output a person reads, not for programmatic
branching.

Auth errors, rate limits/quota, and 5xx are all detected the same way for
every provider (HTTP status code) and need no maintenance. What genuinely
differs by provider — and is the part most likely to need a tweak if a
provider changes their API — is (a) the request/response shape, and
(b) the specific field that signals a *safety refusal* as opposed to a
normal answer (still HTTP 200, but not something to hand back to the
user, and worth failing over on since a different provider may not
refuse the same prompt). Each adapter is short and self-contained
specifically so that if one provider's shape changes, fixing it doesn't
risk the other nine.

Tool calling (added alongside system info tools — see tools.py) follows
the same principle: each adapter runs its own small "ask, maybe get a
tool call, run it, ask again" loop internally, in its own provider's wire
format, rather than funneling everything through one shared cross-
provider tool-message format. More duplication, but a Gemini-shaped bug
still can't touch Anthropic's tool handling. tools/tool_executor are
optional on every call_* function so nothing about the non-tool path
changes for a caller that doesn't pass them.
"""

import json

import requests

MAX_TOOL_ROUNDS = 4  # follow-up requests allowed after a tool call, per ask — plenty for simple lookups


class AIResult:
    """The outcome of one call to one provider.

    ``tool_history`` is set when a provider ran tools before failing — it's a
    list of generic {role, content} messages (system+user+tool rounds) that the
    next provider can continue from instead of re-running those tools.
    """

    __slots__ = ("ok", "text", "error", "tool_history")

    def __init__(self, ok, text=None, error=None, tool_history=None):
        self.ok = ok
        self.text = text
        self.error = error
        self.tool_history = tool_history  # enriched messages to hand to the next provider


def _post_json(url, headers, payload, timeout):
    """POST and return (response, None) or (None, human-readable error) —
    never raises. Every network-level failure (DNS, refused connection,
    timeout, TLS, ...) collapses into the second case so adapters don't
    each need their own except-block zoo."""
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, f"timed out after {timeout}s"
    except requests.exceptions.ConnectionError:
        return None, "couldn't connect (network issue, or the service is down)"
    except requests.exceptions.RequestException as e:
        return None, f"request failed: {e}"
    return resp, None


def _status_reason(resp):
    """None for a 2xx response; otherwise a short human reason."""
    if 200 <= resp.status_code < 300:
        return None
    if resp.status_code in (401, 403):
        return f"invalid or unauthorized API key (HTTP {resp.status_code})"
    if resp.status_code == 429:
        return "rate limited or quota exceeded (HTTP 429)"
    if resp.status_code == 402:
        return "payment required — out of credits (HTTP 402)"
    if 500 <= resp.status_code < 600:
        return f"provider server error (HTTP {resp.status_code})"
    snippet = (resp.text or "").strip().replace("\n", " ")[:180]
    return f"HTTP {resp.status_code}{': ' + snippet if snippet else ''}"


def _parse_json(resp):
    try:
        return resp.json(), None
    except ValueError:
        return None, "couldn't parse the response as JSON"


def _split_system(messages):
    """Anthropic and Gemini both want the system prompt out-of-band, not as
    a message with role 'system'. Returns (system_text, other_messages)."""
    system_parts = []
    turns = []
    for m in messages:
        if m.get("role") == "system":
            if m.get("content"):
                system_parts.append(m["content"])
        else:
            turns.append(m)
    return "\n\n".join(system_parts), turns


def _call_tool_safely(tool_executor, name, arguments):
    """Call the caller's tool_executor and return whatever it returns
    (usually a dict) — or a small error dict if it raised. Never lets a
    broken tool take down the request loop."""
    try:
        return tool_executor(name, arguments)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


def _stringify_tool_result(result):
    """Most providers want the tool result as a plain string. Structured
    (dict/list) results get JSON-encoded so the model can still read the
    individual fields precisely rather than a mangled repr()."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except TypeError:
        return str(result)


def _decode_arguments(raw_args):
    """Tool-call arguments arrive as a JSON *string* from most providers'
    REST APIs, but already-decoded as a dict from some client libraries /
    Ollama's native endpoint. Handle both, defaulting to {} on anything
    unparseable rather than raising."""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            decoded = json.loads(raw_args)
            return decoded if isinstance(decoded, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _give_up_error():
    return f"gave up after {MAX_TOOL_ROUNDS} rounds of tool calls with no final answer"


def _json_object_at(s, start):
    """Parse a JSON object starting at s[start]. Returns (obj, end_index) or (None, start)."""
    if start >= len(s) or s[start] != "{":
        return None, start
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                raw = s[start : i + 1]
                try:
                    return json.loads(raw), i + 1
                except json.JSONDecodeError:
                    return None, start
    return None, start


def _extract_text_tool_calls(text, allowed_names):
    """Groq (and some others) dump tool calls as plain text instead of tool_calls.
    Recover [called name with {...}] and {"name":...,"arguments":...}."""
    if not text or not allowed_names:
        return []
    allowed = set(allowed_names)
    s = text.strip()
    calls = []

    idx = 0
    while True:
        i = s.find("[called ", idx)
        if i < 0:
            break
        rest = s[i + 8 :]
        name = rest.split(None, 1)[0] if rest.split() else ""
        brace = rest.find("{")
        if not name or brace < 0:
            idx = i + 8
            continue
        obj, end = _json_object_at(rest, brace)
        if obj is not None and name in allowed:
            calls.append((name, obj if isinstance(obj, dict) else {}))
        idx = i + 8 + (end if obj is not None else brace + 1)

    if calls:
        return calls

    try:
        blob = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(blob, dict) and blob.get("name") in allowed:
        args = blob.get("arguments") or blob.get("argument") or blob.get("args") or {}
        if isinstance(args, str):
            args = _decode_arguments(args)
        if isinstance(args, dict):
            return [(blob["name"], args)]
    return []


def _openai_messages_to_generic(working_messages):
    """Convert an OpenAI-style working_messages list (which may include
    assistant+tool_call round-trips) into a generic list that any adapter
    can continue from. Tool call objects are serialised to a JSON string
    so the next provider sees the action + result in plain text."""
    out = []
    for m in working_messages:
        role = m.get("role", "")
        if role == "system":
            out.append({"role": "system", "content": m.get("content", "")})
        elif role == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                # Anthropic tool_result blocks — flatten to text
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        parts.append(b.get("content") or b.get("text") or "")
                    else:
                        parts.append(str(b))
                out.append({"role": "user", "content": "\n".join(filter(None, parts))})
            else:
                out.append({"role": "user", "content": content})
        elif role == "assistant":
            content = m.get("content", "")
            tool_calls = m.get("tool_calls")
            if tool_calls:
                # Summarise what was called so the next provider has context
                parts = []
                if content:
                    parts.append(content)
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    parts.append(f"[called {fn.get('name', '?')} with {fn.get('arguments', '{}')}]")
                out.append({"role": "assistant", "content": "\n".join(parts)})
            else:
                out.append({"role": "assistant", "content": content or ""})
        elif role == "tool":
            # Convert tool result to a user message so any provider can read it
            out.append({"role": "user", "content": f"[tool result] {m.get('content', '')}"})
    return out


def _gemini_contents_to_generic(working_contents):
    """Convert Gemini-style contents list back to generic messages."""
    out = []
    for c in working_contents:
        role = c.get("role", "")
        parts = c.get("parts") or []
        generic_role = "assistant" if role == "model" else "user"
        texts = []
        for p in parts:
            if isinstance(p, dict):
                if "text" in p:
                    texts.append(p["text"])
                elif "functionCall" in p:
                    fc = p["functionCall"]
                    texts.append(f"[called {fc.get('name', '?')} with {json.dumps(fc.get('args') or {})}]")
                elif "functionResponse" in p:
                    fr = p["functionResponse"]
                    texts.append(f"[tool result for {fr.get('name', '?')}] {json.dumps(fr.get('response') or {})}")
        if texts:
            out.append({"role": generic_role, "content": "\n".join(texts)})
    return out


def _anthropic_turns_to_generic(system_text, working_turns):
    """Convert Anthropic-style turns list back to generic messages."""
    out = []
    if system_text:
        out.append({"role": "system", "content": system_text})
    for t in working_turns:
        role = t.get("role", "")
        content = t.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    parts.append(f"[called {b.get('name', '?')} with {json.dumps(b.get('input') or {})}]")
                elif b.get("type") == "tool_result":
                    parts.append(f"[tool result] {b.get('content', '')}")
            content = "\n".join(filter(None, parts))
        out.append({"role": role, "content": content})
    return out


# ---------------------------------------------------------------------------
# OpenAI-compatible: OpenAI itself, xAI/Grok, Groq, Mistral, DeepSeek,
# OpenRouter, and (in principle) any other host that mirrors the
# /chat/completions request and response shape. This is deliberately the
# generic "type" — adding a new OpenAI-compatible provider later needs a
# config block, not new code. Tool format: tools: [{type:"function",
# function:{name, description, parameters}}]; a tool call comes back as
# choices[0].message.tool_calls, answered with role:"tool" messages
# carrying the matching tool_call_id.
# ---------------------------------------------------------------------------

def call_openai_compatible(provider, messages, timeout, tools=None, tool_executor=None):
    base_url = provider.get("base_url") or ""
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not base_url:
        return AIResult(False, error="no base_url configured")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    working_messages = list(messages)
    ran_tools = False
    tools_payload = None
    if tools:
        tools_payload = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        payload = {
            "model": model,
            "messages": working_messages,
            "max_tokens": provider.get("max_tokens", 700),
        }
        if tools_payload:
            payload["tools"] = tools_payload
        extra = provider.get("extra_params")
        if isinstance(extra, dict):
            payload.update(extra)

        resp, net_err = _post_json(base_url, headers, payload, timeout)
        if net_err:
            return AIResult(False, error=net_err,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
        reason = _status_reason(resp)
        if reason:
            return AIResult(False, error=reason,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        data, parse_err = _parse_json(resp)
        if parse_err:
            return AIResult(False, error=parse_err,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        choices = data.get("choices") or []
        if not choices:
            return AIResult(False, error="empty response (no choices)",
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
        choice = choices[0]

        if choice.get("finish_reason") == "content_filter":
            return AIResult(False, error="refused by the provider's content filter",
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls")

        if tool_calls and tool_executor and round_num < MAX_TOOL_ROUNDS:
            ran_tools = True
            working_messages.append(message)
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = _decode_arguments(fn.get("arguments"))
                result_text = _stringify_tool_result(_call_tool_safely(tool_executor, name, args))
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result_text,
                })
            continue

        text = (message.get("content") or "").strip()
        if not text:
            refusal = message.get("refusal")
            if refusal:
                return AIResult(False, error=f"refused: {refusal}",
                                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
            if tool_calls:
                return AIResult(False, error=_give_up_error(),
                                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
            return AIResult(False, error="empty response content",
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        allowed = {t.get("name") for t in (tools or []) if t.get("name")}
        text_calls = _extract_text_tool_calls(text, allowed)
        if text_calls and tool_executor and round_num < MAX_TOOL_ROUNDS:
            ran_tools = True
            result_bits = []
            for name, args in text_calls:
                raw = _call_tool_safely(tool_executor, name, args)
                result_bits.append(f"{name}: {_stringify_tool_result(raw)}")
            working_messages.append({
                "role": "user",
                "content": (
                    "Your last message was a tool call written as plain text. It has now been "
                    "executed. Results:\n"
                    + "\n".join(result_bits)
                    + "\nReply to the user in plain language. Only claim a launch/install if "
                    "these results say it succeeded."
                ),
            })
            continue

        return AIResult(True, text=text)

    return AIResult(False, error=_give_up_error(),
                    tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)


# ---------------------------------------------------------------------------
# Anthropic (Claude) — Messages API. Auth via x-api-key + anthropic-version
# headers, system prompt is a top-level field, max_tokens is required, and
# a refusal is a normal HTTP 200 with stop_reason == "refusal". Tool
# format: tools: [{name, description, input_schema}]; a tool call comes
# back as a tool_use content block, answered with a user message holding
# a matching tool_result block (tool_use_id).
# ---------------------------------------------------------------------------

def call_anthropic(provider, messages, timeout, tools=None, tool_executor=None):
    base_url = provider.get("base_url") or "https://api.anthropic.com/v1/messages"
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not api_key:
        return AIResult(False, error="no api_key configured")

    system_text, turns = _split_system(messages)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    working_turns = list(turns)
    ran_tools = False
    tools_payload = None
    if tools:
        tools_payload = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]

    def _history():
        return _anthropic_turns_to_generic(system_text, working_turns) if ran_tools else None

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        payload = {
            "model": model,
            "max_tokens": provider.get("max_tokens", 700),
            "messages": working_turns,
        }
        if system_text:
            payload["system"] = system_text
        if tools_payload:
            payload["tools"] = tools_payload

        resp, net_err = _post_json(base_url, headers, payload, timeout)
        if net_err:
            return AIResult(False, error=net_err, tool_history=_history())
        reason = _status_reason(resp)
        if reason:
            return AIResult(False, error=reason, tool_history=_history())

        data, parse_err = _parse_json(resp)
        if parse_err:
            return AIResult(False, error=parse_err, tool_history=_history())

        if data.get("stop_reason") == "refusal":
            return AIResult(False, error="refused by the model's safety classifier", tool_history=_history())

        blocks = data.get("content") or []
        tool_use_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]

        if tool_use_blocks and tool_executor and round_num < MAX_TOOL_ROUNDS:
            ran_tools = True
            working_turns.append({"role": "assistant", "content": blocks})
            result_blocks = []
            for b in tool_use_blocks:
                result_text = _stringify_tool_result(
                    _call_tool_safely(tool_executor, b.get("name", ""), b.get("input") or {})
                )
                result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": b.get("id", ""),
                    "content": result_text,
                })
            working_turns.append({"role": "user", "content": result_blocks})
            continue

        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
        if not text:
            if tool_use_blocks:
                return AIResult(False, error=_give_up_error(), tool_history=_history())
            return AIResult(False, error="empty response content", tool_history=_history())
        return AIResult(True, text=text)

    return AIResult(False, error=_give_up_error(), tool_history=_history())


# ---------------------------------------------------------------------------
# Google Gemini — generateContent API. Assistant turns use role "model",
# not "assistant"; system prompt is a separate systemInstruction field;
# a block shows up either as promptFeedback.blockReason (the prompt itself
# was blocked) or candidates[0].finishReason (the *answer* was blocked).
# Tool format: tools: [{function_declarations: [{name, description,
# parameters}]}], where parameters' JSON-schema "type" values are
# uppercased (Gemini's documented SDK-level enum form, e.g. "OBJECT" not
# "object") — see _to_gemini_schema. A call comes back as a functionCall
# part; answered with a role:"user" message holding matching
# functionResponse parts (Gemini 2.x only accepts user/model roles).
# ---------------------------------------------------------------------------

_GEMINI_SCHEMA_SKIP = frozenset({
    "additionalProperties",
    "$schema",
    "$id",
    "definitions",
    "default",
    "examples",
    "title",
})


def _to_gemini_schema(schema):
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in _GEMINI_SCHEMA_SKIP:
            continue
        if k == "type" and isinstance(v, str):
            out[k] = v.upper()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _to_gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _to_gemini_schema(v)
        elif k == "required" and isinstance(v, list):
            out[k] = v
        elif k in ("description", "enum"):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _to_gemini_schema(v)
        elif isinstance(v, list):
            out[k] = [_to_gemini_schema(i) if isinstance(i, dict) else i for i in v]
    if out.get("type") == "ARRAY" and "items" not in out:
        out["items"] = {"type": "OBJECT"}
    return out


def call_gemini(provider, messages, timeout, tools=None, tool_executor=None):
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not api_key:
        return AIResult(False, error="no api_key configured")

    base_url = provider.get("base_url") or (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )
    url = base_url.format(model=model) if "{model}" in base_url else base_url

    system_text, turns = _split_system(messages)

    def _to_gemini_content(m):
        role = m.get("role", "")
        content = m.get("content", "")
        gemini_role = "model" if role == "assistant" else "user"
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    parts.append({"text": b.get("content") or b.get("text") or ""})
                else:
                    parts.append({"text": str(b)})
            return {"role": gemini_role, "parts": parts}
        return {"role": gemini_role, "parts": [{"text": content}]}

    working_contents = [_to_gemini_content(t) for t in turns]
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    ran_tools = False
    tools_payload = None
    if tools:
        tools_payload = [{"function_declarations": [
            {"name": t["name"], "description": t["description"], "parameters": _to_gemini_schema(t["parameters"])}
            for t in tools
        ]}]

    def _history():
        if not ran_tools:
            return None
        generic = _gemini_contents_to_generic(working_contents)
        if system_text:
            generic.insert(0, {"role": "system", "content": system_text})
        return generic

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        payload = {
            "contents": working_contents,
            "generationConfig": {"maxOutputTokens": provider.get("max_tokens", 700)},
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools_payload:
            payload["tools"] = tools_payload

        resp, net_err = _post_json(url, headers, payload, timeout)
        if net_err:
            return AIResult(False, error=net_err, tool_history=_history())
        reason = _status_reason(resp)
        if reason:
            return AIResult(False, error=reason, tool_history=_history())

        data, parse_err = _parse_json(resp)
        if parse_err:
            return AIResult(False, error=parse_err, tool_history=_history())

        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            return AIResult(False, error=f"blocked by provider safety filter ({block_reason})", tool_history=_history())

        candidates = data.get("candidates") or []
        if not candidates:
            return AIResult(False, error="empty response (no candidates)", tool_history=_history())
        candidate = candidates[0]

        finish_reason = candidate.get("finishReason")
        if finish_reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            return AIResult(False, error=f"blocked by provider safety filter ({finish_reason})", tool_history=_history())

        parts = (candidate.get("content") or {}).get("parts") or []
        call_parts = [p for p in parts if isinstance(p, dict) and "functionCall" in p]

        if call_parts and tool_executor and round_num < MAX_TOOL_ROUNDS:
            ran_tools = True
            working_contents.append({"role": "model", "parts": parts})
            response_parts = []
            for p in call_parts:
                fc = p["functionCall"]
                raw_result = _call_tool_safely(tool_executor, fc.get("name", ""), fc.get("args") or {})
                response_obj = raw_result if isinstance(raw_result, dict) else {"result": raw_result}
                fr = {"name": fc.get("name", ""), "response": response_obj}
                if fc.get("id"):
                    fr["id"] = fc["id"]
                response_parts.append({"functionResponse": fr})
            working_contents.append({"role": "user", "parts": response_parts})
            continue

        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if not text:
            if call_parts:
                return AIResult(False, error=_give_up_error(), tool_history=_history())
            return AIResult(False, error="empty response content", tool_history=_history())
        return AIResult(True, text=text)

    return AIResult(False, error=_give_up_error(), tool_history=_history())


# ---------------------------------------------------------------------------
# Cohere — Chat API v2. Closer to the OpenAI shape than Anthropic/Gemini
# (a flat "messages" list with role/content, system role included inline),
# but the response nests the reply under message.content[0].text. Tool
# format mirrors OpenAI's (tools: [{type:"function", function:{...}}],
# tool_calls on the response) — this is the one adapter built without a
# confirmed example of the tool_result round-trip specifically, so if
# Cohere ever errors here specifically, this request-building block is
# the first place to check against their current docs.
# ---------------------------------------------------------------------------

def call_cohere(provider, messages, timeout, tools=None, tool_executor=None):
    base_url = provider.get("base_url") or "https://api.cohere.com/v2/chat"
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not api_key:
        return AIResult(False, error="no api_key configured")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    working_messages = list(messages)
    ran_tools = False
    tools_payload = None
    if tools:
        tools_payload = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        payload = {
            "model": model,
            "messages": working_messages,
            "max_tokens": provider.get("max_tokens", 700),
        }
        if tools_payload:
            payload["tools"] = tools_payload

        resp, net_err = _post_json(base_url, headers, payload, timeout)
        if net_err:
            return AIResult(False, error=net_err,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
        reason = _status_reason(resp)
        if reason:
            return AIResult(False, error=reason,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        data, parse_err = _parse_json(resp)
        if parse_err:
            return AIResult(False, error=parse_err,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        message = data.get("message") or {}
        tool_calls = message.get("tool_calls")

        if tool_calls and tool_executor and round_num < MAX_TOOL_ROUNDS:
            ran_tools = True
            working_messages.append(message)
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = _decode_arguments(fn.get("arguments"))
                result_text = _stringify_tool_result(_call_tool_safely(tool_executor, name, args))
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result_text,
                })
            continue

        content = message.get("content") or []
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
        if not text:
            if tool_calls:
                return AIResult(False, error=_give_up_error(),
                                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
            return AIResult(False, error="empty response content",
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
        return AIResult(True, text=text)

    return AIResult(False, error=_give_up_error(),
                    tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)


# ---------------------------------------------------------------------------
# Ollama — local models, no API key, nothing leaves your machine. Great
# last-resort fallback: put it last in the provider order and it can never
# "run out" the way a paid API can, as long as it's installed and running.
# Tool format mirrors OpenAI's, with one real difference: tool_calls'
# arguments come back already decoded as an object, not a JSON string
# (_decode_arguments handles either). Only models whose chat template
# supports tools will actually use them — see the "Tools" badge on a
# model's ollama.com library page.
# ---------------------------------------------------------------------------

def call_ollama(provider, messages, timeout, tools=None, tool_executor=None):
    base_url = provider.get("base_url") or "http://localhost:11434/api/chat"
    model = provider.get("model") or ""

    headers = {"Content-Type": "application/json"}
    working_messages = list(messages)
    ran_tools = False
    tools_payload = None
    if tools:
        tools_payload = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        payload = {"model": model, "messages": working_messages, "stream": False}
        if tools_payload:
            payload["tools"] = tools_payload

        resp, net_err = _post_json(base_url, headers, payload, timeout)
        if net_err:
            return AIResult(False, error=f"{net_err} (is Ollama installed and running?)",
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
        reason = _status_reason(resp)
        if reason:
            return AIResult(False, error=reason,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        data, parse_err = _parse_json(resp)
        if parse_err:
            return AIResult(False, error=parse_err,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        message = data.get("message") or {}
        tool_calls = message.get("tool_calls")

        if tool_calls and tool_executor and round_num < MAX_TOOL_ROUNDS:
            ran_tools = True
            working_messages.append(message)
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = _decode_arguments(fn.get("arguments"))
                result_text = _stringify_tool_result(_call_tool_safely(tool_executor, name, args))
                working_messages.append({"role": "tool", "content": result_text})
            continue

        text = (message.get("content") or "").strip()
        if not text:
            if tool_calls:
                return AIResult(False, error=_give_up_error(),
                                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
            return AIResult(
                False,
                error="empty response content (is the model pulled? try: ollama pull " + (model or "<model>") + ")",
                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None,
            )
        return AIResult(True, text=text)

    return AIResult(False, error=_give_up_error(),
                    tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)


ADAPTERS = {
    "openai_compatible": call_openai_compatible,
    "anthropic": call_anthropic,
    "gemini": call_gemini,
    "cohere": call_cohere,
    "ollama": call_ollama,
}
