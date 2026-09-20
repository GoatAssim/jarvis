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
import re
import threading

import requests

from . import logs, prompt_cache
from . import token_usage

MAX_TOOL_ROUNDS = 5  # follow-up requests allowed after a tool call, per ask — plenty for simple
# lookups. Every round resends the *whole* growing transcript (system prompt, history, every
# tool call/result so far), so this is the single biggest token-cost lever in this file: raising
# it doesn't add tokens linearly, it adds them roughly quadratically (round N resends rounds
# 1..N-1 too). 5 still covers a two-step "search then fetch then answer" with room to spare.

GLOBAL_MAX_TOOL_ROUNDS = MAX_TOOL_ROUNDS + 1  # 6: hard cap across an ENTIRE ask(), spanning
# every provider/key failover attempt — not just one attempt's MAX_TOOL_ROUNDS. Without this,
# a key that 429s after burning its 5 rounds hands the (now much bigger) transcript to the next
# key, which gets its own fresh 5 rounds — the loop counter resets but the tool-call history it's
# resending every round does not. See jarvis-token-optimization-handoff.md.


class RoundBudget:
    """Cross-attempt tool-round counter shared by every provider/key tried for one ask().

    Each adapter still enforces its own per-attempt MAX_TOOL_ROUNDS (so a single
    well-behaved attempt is untouched), but every actual tool-call round — regardless
    of which key or provider is currently active — also draws down this shared pool.
    Once it's empty, take() starts returning False and every adapter treats that
    exactly like hitting its own local round cap: stop calling tools, answer with
    whatever it has (or give up cleanly) instead of quietly starting a fresh 5-round
    budget on the next failover.
    """

    def __init__(self, limit=GLOBAL_MAX_TOOL_ROUNDS, grace=False):
        self.limit = limit
        self.used = 0
        # F.1 "grace call": ONE extra, last-chance tool round for the whole
        # ask() once the budget above is spent — see _forced_ending(). Off by
        # default so every caller that builds its own RoundBudget (code_agent's
        # inner loop, tests) keeps exactly the budget it asked for; only
        # ai_client.ask() turns it on, and only via defaults.grace_call.
        self.grace = bool(grace)
        self.grace_used = False

    def remaining(self):
        return max(0, self.limit - self.used)

    def take(self):
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    def grace_available(self):
        return self.grace and not self.grace_used

    def take_grace(self):
        """True at most once per budget (i.e. once per ask(), across every
        provider/key failover), and only if grace was enabled."""
        if not self.grace_available():
            return False
        self.grace_used = True
        return True



# ---------------------------------------------------------------------------
# Logging context — the "Logs" feature. ai_client.py sets this once per
# provider attempt (see ai_client.ask) so that the shared low-level helpers
# below (_post_json, _call_tool_safely) can attribute the raw request/
# response/tool traffic to the right conversation without every one of the
# five adapter loops having to thread a conv_id argument through by hand.
# Thread-local since the web server may serve multiple asks concurrently.
# ---------------------------------------------------------------------------
_log_local = threading.local()


def set_log_context(conv_id, provider=None, on_tool_usage=None, base_url=None):
    _log_local.conv_id = conv_id
    _log_local.provider = provider
    # F.12: the host this attempt's own requests go to. A request to any OTHER
    # host during the attempt (the AI risk review runs on a different provider
    # while a tool call is being confirmed) is logged under that host, not
    # under this attempt's key label — see _log_provider_for().
    _log_local.host = _host_of_url(base_url)
    # on_tool_usage(name, input_tokens, output_tokens), if given, fires once
    # per tool call, right after _call_tool_safely below has both halves of
    # the token estimate (it can't fire any earlier — output_tokens isn't
    # known until the tool has actually returned).
    _log_local.on_tool_usage = on_tool_usage
    # Phase 0 (see new_plan.md): usage/round bookkeeping is scoped to one
    # provider attempt, same lifecycle as conv_id/provider above — reset
    # here (ai_client.ask calls this once per attempt) and read back via
    # get_usage_summary() once that attempt either succeeds or fails.
    _log_local.usage_rounds = []
    _log_local.tool_usage = []


# ---------------------------------------------------------------------------
# Thinking context. Set by ai_client.ask() per attempt, read by every adapter.
#
# A module-level holder rather than a parameter on five adapter signatures,
# for the same reason set_log_context() is one: the adapters are called
# through a uniform ADAPTERS[type](provider, messages, timeout, ...) contract
# that other callers (tests, benchmark_pc_actions.py) also use, and widening
# that signature would break every one of them for a field most don't care
# about. Cleared in the same finally: block as the log context.
# ---------------------------------------------------------------------------
_thinking = {"level": "off", "trace": "", "rounds": 0, "requested": 0}


def set_thinking(level="off"):
    _thinking["level"] = level or "off"
    _thinking["trace"] = ""
    _thinking["rounds"] = 0
    _thinking["requested"] = 0


def get_thinking_trace():
    """The concatenated thinking text from the attempt that just ran, plus
    how many rounds actually carried a thinking request — which is what lets
    the caller show "thought on 2 of 5 rounds" instead of implying every
    round paid for it."""
    return {"text": _thinking.get("trace", ""),
            "rounds": _thinking.get("rounds", 0),
            "requested": _thinking.get("requested", 0),
            "level": _thinking.get("level", "off")}


def _apply_thinking(payload, provider, provider_type, round_num, ran_tools, thought_rounds):
    """Merge this round's thinking keys into `payload`. Returns True if any
    were added, so the adapter can count the round.

    The whole token-discipline decision lives in reasoning.round_patch —
    see its comment block. Here it's just: ask, merge, count.
    """
    from . import reasoning

    # A forced ending (see _forced_ending) is a plain "finish up" request that
    # re-enters the adapter at round 0; without this it would look like a fresh
    # ask and pay for a whole new thinking pass on the last, cheapest call.
    if getattr(_log_local, "forced", None) is not None:
        return False
    level = _thinking.get("level", "off")
    if reasoning.normalize_level(level) == "off":
        return False
    # Round 0 always. After that, exactly one more — the first round after a
    # tool batch came back, where the model is synthesizing results rather
    # than picking the next call.
    if round_num != 0 and not (ran_tools and thought_rounds < 2):
        return False

    patch = reasoning.round_patch(
        provider_type, level, round_num=round_num,
        is_final=bool(ran_tools and round_num > 0), ran_tools=ran_tools,
        provider_name=provider.get("name") or "", model=provider.get("model") or "",
        base_url=provider.get("base_url") or "",
    )
    if not patch:
        return False

    # Gemini nests its config; everything else is top-level.
    gen = patch.pop("_generationConfig", None)
    if gen:
        target = payload.setdefault("generationConfig", {})
        for key, value in gen.items():
            target.setdefault(key, value)
    payload.update(patch)

    if provider_type == "anthropic":
        # budget_tokens must be strictly less than max_tokens or the request
        # is rejected — raise the ceiling rather than shrink the budget, or
        # asking for `high` against a 700-token cap silently buys nothing.
        payload["max_tokens"] = reasoning.fit_max_tokens(
            payload.get("max_tokens"), level)
        # Anthropic rejects a non-default temperature alongside thinking.
        payload.pop("temperature", None)
        payload.pop("top_p", None)

    _thinking["requested"] += 1
    return True


def _collect_thinking(provider_type, data):
    from . import reasoning

    text = reasoning.extract_trace(provider_type, data)
    if not text:
        return
    _thinking["rounds"] += 1
    existing = _thinking.get("trace") or ""
    # Cheap join, capped: a runaway thinking block must not be able to grow
    # this without bound before ai_client ever gets to clip it.
    if len(existing) < 20000:
        _thinking["trace"] = (existing + "\n\n" + text).strip() if existing else text


def clear_log_context():
    _log_local.conv_id = None
    _log_local.provider = None
    _log_local.on_tool_usage = None
    _log_local.forced = None


def _log_conv_id():
    return getattr(_log_local, "conv_id", None)


def _log_provider():
    return getattr(_log_local, "provider", None)


def _host_of_url(url):
    from urllib.parse import urlparse
    try:
        return urlparse(str(url or "")).netloc or None
    except ValueError:
        return None


def _log_provider_for(url):
    """The label a request to `url` should be logged under. Normally the
    attempt's own key label. But a Groq risk-review request made while
    `gemini (key 2/10)` was the active attempt used to be logged AS
    `gemini (key 2/10)` (F.12), so the Logs viewer credited Gemini's key with
    Groq's tokens."""
    label = _log_provider()
    attempt_host = getattr(_log_local, "host", None)
    host = _host_of_url(url)
    if label and attempt_host and host and host != attempt_host:
        return f"{host} (side request during {label})"
    return label


def _log_cache_plan(plan, provider_type):
    """Record what prompt_cache.plan() decided, once per attempt.

    Caching fails silently by design — a prefix that never matches produces
    a correct answer at full price, with no error anywhere. This line is the
    only thing standing between that and an unnoticed regression, so it logs
    the human-readable `reason` string rather than just the flags.
    """
    conv_id = _log_conv_id()
    if not conv_id or not isinstance(plan, dict):
        return
    logs.log(
        conv_id, "prompt_cache",
        {
            "provider_type": provider_type,
            "eligible": plan.get("eligible"),
            "reason": plan.get("reason"),
            "system_breakpoint": plan.get("system_index") is not None,
            "tools_breakpoint": bool(plan.get("tools")),
            "ttl": plan.get("ttl"),
        },
        provider=_log_provider(), round_num=0,
    )


def _record_usage(provider_type, data, round_num):
    """Phase 0: log + accumulate the real (provider-reported) token usage
    for one request/response round, if the response carried any."""
    usage = token_usage.extract_usage(provider_type, data)
    if usage is None:
        return
    # Inside a forced ending the adapter is re-entered at round 0; label the
    # entry with the real round so the usage breakdown reads 5, 6 (not 0, 0).
    round_num = round_num + ((getattr(_log_local, "forced", None) or {}).get("round_base", 0))
    entry = {"round": round_num, **usage}
    rounds = getattr(_log_local, "usage_rounds", None)
    if rounds is None:
        rounds = []
        _log_local.usage_rounds = rounds
    rounds.append(entry)
    conv_id = _log_conv_id()
    if conv_id:
        logs.log(conv_id, "usage", entry, provider=_log_provider(), round_num=round_num)


def get_usage_summary():
    """Totals + a per-round/per-tool-call breakdown for the attempt
    currently (or most recently) in log-context scope. Real provider
    counts where the response reported them; ~estimated (see
    token_usage.py) for tool-call arguments/results, which no provider
    reports token counts for on its own."""
    rounds = getattr(_log_local, "usage_rounds", None) or []
    tool_calls = getattr(_log_local, "tool_usage", None) or []
    total_input = sum(r.get("input_tokens") or 0 for r in rounds)
    total_output = sum(r.get("output_tokens") or 0 for r in rounds)
    summary = {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "rounds": rounds,
        "tool_calls": tool_calls,
    }
    # Prompt-cache totals (see prompt_cache.py). Included only when at
    # least one round actually reported cache activity, so a provider that
    # never caches — or a model below its cacheable floor — reads exactly
    # as it did before rather than showing a permanent, meaningless zero.
    # cache_read_tokens are already counted inside input_tokens above;
    # these are a breakdown of that number, not an addition to it.
    cache_read = sum(r.get("cache_read_tokens") or 0 for r in rounds)
    cache_write = sum(r.get("cache_write_tokens") or 0 for r in rounds)
    if cache_read or cache_write:
        summary["cache_read_tokens"] = cache_read
        summary["cache_write_tokens"] = cache_write
    return summary


class AIResult:
    """The outcome of one call to one provider.

    ``tool_history`` is set when a provider ran tools before failing — it's a
    list of generic {role, content} messages (system+user+tool rounds) that the
    next provider can continue from instead of re-running those tools.
    """

    __slots__ = ("ok", "text", "error", "tool_history", "usage", "kind", "pending")

    def __init__(self, ok, text=None, error=None, tool_history=None, usage=None, kind=None, pending=None):
        self.ok = ok
        self.text = text
        self.error = error
        self.tool_history = tool_history  # enriched messages to hand to the next provider
        self.usage = usage  # Phase 0 (new_plan.md): get_usage_summary() for this attempt
        # F.8: WHY an attempt failed, one of the KIND_* values below. Derived
        # from the error text unless a call site knows better, so the ~60
        # existing `AIResult(False, error=...)` sites needed no edit.
        self.kind = kind if kind else (None if ok else classify_failure(error))
        # F.1: the tool call(s) (name, args) the model still wanted to make
        # when it could not be given tools. None unless kind == KIND_BUDGET.
        self.pending = list(pending) if pending else None


# ---------------------------------------------------------------------------
# Failure kinds (master plan F.8).
#
# Before this, every failure was a string and ask() rotated to the next key
# after almost all of them. That is right for a dead/rate-limited key and
# wrong for KIND_BUDGET: "the tool-round budget is spent and the model still
# wants a tool" is not the key's fault, every other key will do the same, and
# in the logs it turned one missing step into ten wasted keys. The kinds are
# derived from the error text (classify_failure) so no call site had to change.
#
# Only two kinds change what ask() does today: KIND_BUDGET (never rotate, end
# through a harness-written reply) and KIND_SHAPE (already handled by
# is_request_shape_error). The rest are named so the next changes (F.9's key
# health, F.7's failover) have one classifier to build on instead of a fourth
# copy of the string matching.
# ---------------------------------------------------------------------------
KIND_BUDGET = "budget"        # tools were withheld and the model still wanted one
KIND_KEY = "key"              # bad / rate-limited / out-of-credit key: another key can help
KIND_OVERLOAD = "overload"    # provider-side 5xx: model-wide, another key rarely helps
KIND_NETWORK = "network"      # timeout / refused connection / DNS
KIND_SHAPE = "shape"          # the request itself was rejected; no key can fix it
KIND_EMPTY = "empty"          # a reply with nothing in it
KIND_MALFORMED = "malformed"  # the model tried to call a tool and garbled it
KIND_REFUSED = "refused"      # safety filter / refusal
KIND_OTHER = "other"


def classify_failure(error):
    """One of the KIND_* values for an AIResult.error string (or KIND_OTHER)."""
    if not error:
        return KIND_OTHER
    e = str(error).lower()
    if "gave up after" in e and "tool calls" in e:
        return KIND_BUDGET
    if "malformed tool call" in e or "malformed_function_call" in e:
        return KIND_MALFORMED
    if is_request_shape_error(e):
        return KIND_SHAPE
    if ("rate limited" in e or "quota exceeded" in e or "invalid or unauthorized api key" in e
            or "payment required" in e or "out of credits" in e or "no api_key configured" in e):
        return KIND_KEY
    if "provider server error" in e:
        return KIND_OVERLOAD
    if "timed out" in e or "couldn't connect" in e or "request failed" in e:
        return KIND_NETWORK
    if "empty response" in e or "hit max_tokens with no visible output" in e:
        return KIND_EMPTY
    if "refused" in e or "blocked by provider safety filter" in e or "content filter" in e:
        return KIND_REFUSED
    return KIND_OTHER


def _post_json(url, headers, payload, timeout):
    """POST and return (response, None) or (None, human-readable error) —
    never raises. Every network-level failure (DNS, refused connection,
    timeout, TLS, ...) collapses into the second case so adapters don't
    each need their own except-block zoo."""
    conv_id = _log_conv_id()
    if conv_id:
        logs.log(conv_id, "request", {"url": url, "payload": payload}, provider=_log_provider_for(url))
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        err = f"timed out after {timeout}s"
        if conv_id:
            logs.log(conv_id, "error", {"error": err}, provider=_log_provider_for(url))
        return None, err
    except requests.exceptions.ConnectionError:
        err = "couldn't connect (network issue, or the service is down)"
        if conv_id:
            logs.log(conv_id, "error", {"error": err}, provider=_log_provider_for(url))
        return None, err
    except requests.exceptions.RequestException as e:
        err = f"request failed: {e}"
        if conv_id:
            logs.log(conv_id, "error", {"error": err}, provider=_log_provider_for(url))
        return None, err
    if conv_id:
        try:
            body = resp.json()
        except ValueError:
            body = (resp.text or "")[:2000]
        logs.log(conv_id, "response", {"status": resp.status_code, "body": body}, provider=_log_provider_for(url))
    return resp, None


def _status_reason(resp):
    """None for a 2xx response; otherwise a short human reason."""
    if 200 <= resp.status_code < 300:
        return None
    if resp.status_code in (401, 403):
        return f"invalid or unauthorized API key (HTTP {resp.status_code})"
    if resp.status_code == 429:
        # F.9: carry the delay the provider stated in the message, so ask() can
        # park the key for exactly that long (key_health.parse_retry_delay).
        from . import key_health
        delay = key_health.parse_retry_delay(getattr(resp, "text", "") or "", getattr(resp, "headers", None))
        hint = f" [retry in {delay:g}s]" if delay is not None else ""
        return f"rate limited or quota exceeded (HTTP 429){hint}"
    if resp.status_code == 402:
        return "payment required — out of credits (HTTP 402)"
    if 500 <= resp.status_code < 600:
        return f"provider server error (HTTP {resp.status_code})"
    snippet = (resp.text or "").strip().replace("\n", " ")[:180]
    return f"HTTP {resp.status_code}{': ' + snippet if snippet else ''}"


# ---------------------------------------------------------------------------
# Request-shape failures vs key failures.
#
# ai_client.ask() rotates to the provider's next API key after ANY failure,
# which is right for a dead/rate-limited/out-of-credit key and pointless for
# a request the provider rejects on its merits: the same payload gets the same
# 400 from every other key. One real log burned three Groq keys on a single
# "Tool choice is none, but model called a tool" and a fourth on a
# tool-argument schema mismatch before falling through to the next provider.
#
# This is a deliberate WHITELIST of observed message shapes, not "any HTTP
# 400": Gemini reports an invalid API key as a 400 too, and that one IS
# fixed by the next key. An unrecognized failure keeps the old behavior.
# ---------------------------------------------------------------------------
_REQUEST_SHAPE_MARKERS = (
    "tool choice is none",                 # Groq: final round sent without tools, model called one anyway
    "tool call validation failed",         # Groq: model's arguments rejected by the tool's schema
    "did not match schema",                # same family, other wording
    "which was not in request.tools",      # Groq: called a tool that wasn't offered this round
    "duplicate function declaration",      # Gemini: same tool declared twice
)


def is_request_shape_error(reason):
    """True when `reason` (an AIResult.error string) describes a rejection of
    the request itself, which no other key on the same provider can cure."""
    if not reason:
        return False
    lowered = str(reason).lower()
    return any(marker in lowered for marker in _REQUEST_SHAPE_MARKERS)


# ---------------------------------------------------------------------------
# Null-tolerant optional parameters.
#
# A model that "omits" an optional argument will often send an explicit null
# instead. Most hosts don't care, but Groq validates tool-call arguments
# server-side against the schema jarvis sent, so {"parent_id": null} against
# {"type": "string"} is a 400 before jarvis ever sees the call — and, per
# is_request_shape_error above, a 400 no other key can fix.
#
# Widening at the wire boundary (rather than editing ~290 optional parameters
# across the catalog by hand) keeps the source schemas untouched for every
# other adapter. The matching half is tools._drop_null_optionals, which makes
# such a null reach the handler as "argument omitted".
# ---------------------------------------------------------------------------
NULLABLE_OPTIONAL_PROVIDER_NAMES = ("groq",)
_NULLABLE_JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})


def provider_wants_nullable_optionals(provider):
    """Groq by default (by name or host); an explicit boolean
    "nullable_optional_params" in the provider block wins either way."""
    provider = provider or {}
    explicit = provider.get("nullable_optional_params")
    if isinstance(explicit, bool):
        return explicit
    name = str(provider.get("name") or "").lower()
    base_url = str(provider.get("base_url") or "").lower()
    return name.startswith(NULLABLE_OPTIONAL_PROVIDER_NAMES) or "api.groq.com" in base_url


def nullable_optional_parameters(parameters):
    """Copy of a JSON-schema `parameters` object in which every top-level
    OPTIONAL property also accepts null. Required properties, properties
    without a plain string `type`, and anything already nullable are left
    alone. An enum gains None so null doesn't fail the enum instead. Returns
    the input object itself, unchanged, when there is nothing to widen."""
    if not isinstance(parameters, dict):
        return parameters
    props = parameters.get("properties")
    if not isinstance(props, dict) or not props:
        return parameters
    required = set(parameters.get("required") or [])
    widened = {}
    changed = False
    for name, prop in props.items():
        ptype = prop.get("type") if isinstance(prop, dict) else None
        if name in required or not isinstance(ptype, str) or ptype not in _NULLABLE_JSON_TYPES:
            widened[name] = prop
            continue
        new_prop = dict(prop)
        new_prop["type"] = [ptype, "null"]
        enum = new_prop.get("enum")
        if isinstance(enum, list) and None not in enum:
            new_prop["enum"] = list(enum) + [None]
        widened[name] = new_prop
        changed = True
    if not changed:
        return parameters
    out = dict(parameters)
    out["properties"] = widened
    return out


def active_provider_label():
    """Label of the provider/key attempt currently running on THIS thread
    (e.g. "ollama" or "gemini (key 2/10)"), or None outside an ask(). Lets a
    tool handler ask "which model is driving me right now" without
    ToolContext growing a field for it."""
    return _log_provider()


def _parse_json(resp):
    try:
        return resp.json(), None
    except ValueError:
        return None, "couldn't parse the response as JSON"


def _system_parts(messages):
    """Split messages into (ordered list of system strings, other messages).

    ai_client._build_messages() emits the system prompt as MORE THAN ONE
    role:"system" message now — index 0 is the static prefix (persona, tool
    blurb, workflow guidance) and index 1, when present, is the
    per-request tail (memory context keyed on the user's message, saved
    commands, frequency stats). Keeping them as separate strings is what
    lets call_anthropic() put a cache breakpoint between them; see
    prompt_cache.py's module docstring for why the boundary is there.

    Everything downstream that wants the old single string just joins these
    with "\\n\\n" — _split_system() and _merge_system() below both do, which
    is why this split is invisible to every provider that doesn't opt in.
    """
    system_parts = []
    turns = []
    for m in messages:
        if m.get("role") == "system":
            if m.get("content"):
                system_parts.append(m["content"])
        else:
            turns.append(m)
    return system_parts, turns


def _split_system(messages):
    """Anthropic and Gemini both want the system prompt out-of-band, not as
    a message with role 'system'. Returns (system_text, other_messages).

    Unchanged behavior: several system messages join with "\\n\\n", which is
    the exact separator _system_prompt() already uses between its own
    sections, so a two-block system prompt renders byte-identically to the
    single-block one it replaced."""
    system_parts, turns = _system_parts(messages)
    return "\n\n".join(system_parts), turns


# ---------------------------------------------------------------------------
# Prior-tool-result compaction (the O(N^2) round-loop leak)
# ---------------------------------------------------------------------------
# Every adapter below drives a multi-round tool loop by APPENDING to
# working_messages: the assistant's tool_calls message, then one message per
# tool result, then round-tripping the whole list again. Nothing ever trims
# it, so round 5 resends rounds 1-4's tool results verbatim. Measured on a
# realistic 5-round ask with ~1.8KB shaped tool results: 6,601 prompt tokens
# billed, of which 4,966 were re-sent copies of results the model had
# already seen. The cost is quadratic in tool-result bytes, and it lands
# hardest on exactly the multi-step tasks that need the rounds.
#
# The model genuinely does need to see what earlier tools returned — it is
# reasoning about them — so the fix is not deletion. It is the same trick
# ai_client._tool_runs_note() already applies (enhancement #8) when it
# rebuilds a prompt on failover: re-shape an old result down to the current
# verbosity instead of resending it at the size it was first captured.
# That machinery existed but was only reachable on a failover rebuild,
# never inside a successful attempt's own round loop.
#
# Two invariants this must not break:
#   * NEVER remove a message. An OpenAI-style assistant/tool_calls message
#     must be followed by a tool message per call id; dropping one is a hard
#     400. Only `content` is rewritten, in place, never the structure.
#   * The MOST RECENT results stay untouched. The model is actively working
#     with them this round; trimming those would trade tokens for wrong
#     answers, which is a bad trade at any price.

# Characters of an older tool result to keep. Generous enough that a JSON
# result keeps its shape and its first records, small enough that five of
# them cost about as much as one untrimmed one.
PRIOR_RESULT_CHARS = 400

# How many of the newest tool results to leave completely alone. One full
# round's worth: the results the current round is reasoning about.
KEEP_FULL_RESULTS = 2

# Suffix marking an already-trimmed result, which is also what makes the
# trim idempotent and what tells the model the text it is reading is not
# the whole result.
_TRIM_MARKER = "\u2026[trimmed]"


def _tool_result_slots(working):
    """Every place a tool result's text lives, across all three payload
    shapes, as (setter, current_text) pairs in chronological order.

    Three adapters, three shapes, one trimmer — the alternative is three
    near-identical trimmers that drift apart the first time one provider's
    format changes:

      openai / cohere / ollama  {"role": "tool", "content": "..."}
      anthropic                 {"role": "user", "content": [
                                    {"type": "tool_result", "content": "..."}]}
      gemini                    {"role": "user", "parts": [
                                    {"functionResponse": {"response": {...}}}]}
    """
    slots = []
    for message in working:
        if not isinstance(message, dict):
            continue

        if message.get("role") == "tool" and isinstance(message.get("content"), str):
            def setter(value, _m=message):
                _m["content"] = value
            slots.append((setter, message["content"]))
            continue

        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and isinstance(block.get("content"), str)):
                    def setter(value, _b=block):
                        _b["content"] = value
                    slots.append((setter, block["content"]))

        parts = message.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict) or "functionResponse" not in part:
                    continue
                fr = part.get("functionResponse")
                if not isinstance(fr, dict):
                    continue
                response = fr.get("response")
                # Gemini wraps the payload in a dict; the tool text is
                # usually under "result"/"content". Trim whichever string
                # field is actually carrying it rather than assuming one.
                if isinstance(response, dict):
                    for key in ("result", "content", "output", "text"):
                        if isinstance(response.get(key), str):
                            def setter(value, _r=response, _k=key):
                                _r[_k] = value
                            slots.append((setter, response[key]))
                            break
                elif isinstance(response, str):
                    def setter(value, _fr=fr):
                        _fr["response"] = value
                    slots.append((setter, response))
    return slots


def _compact_prior_tool_results(working, keep_full=KEEP_FULL_RESULTS,
                                budget=PRIOR_RESULT_CHARS):
    """Shrink older tool-result payloads in place. Returns chars reclaimed.

    Idempotent: an already-trimmed slot carries the marker suffix and is
    skipped, so calling this once per round — which is what the loops do —
    never re-trims the same text five times.
    """
    slots = _tool_result_slots(working)
    if len(slots) <= keep_full:
        return 0

    reclaimed = 0
    trimmable = slots[:-keep_full] if keep_full else slots
    for setter, text in trimmable:
        if not isinstance(text, str) or len(text) <= budget:
            continue
        if text.endswith(_TRIM_MARKER):
            continue
        reclaimed += len(text) - budget
        setter(text[:budget] + _TRIM_MARKER)
    return reclaimed


def _merge_system(messages):
    """Fold consecutive role:"system" messages into one.

    For the three adapters (openai_compatible, cohere, ollama) that pass
    the caller's message dicts straight into the request payload. Those
    APIs accept several system messages in principle, but not every
    OpenAI-*compatible* host does, and there is no upside to finding out
    the hard way on a user's key — this is the one line that guarantees the
    two-block system prompt is a pure no-op for every provider that isn't
    Anthropic.

    Only leading system messages are merged, matching how _build_messages()
    lays them out; a system message appearing later (nothing does this
    today) is left exactly where it is rather than being hoisted.
    """
    out = []
    leading = []
    rest_started = False
    for m in messages:
        if not rest_started and m.get("role") == "system":
            if m.get("content"):
                leading.append(m["content"])
            continue
        rest_started = True
        out.append(m)
    if leading:
        out.insert(0, {"role": "system", "content": "\n\n".join(leading)})
    return out


def _call_tool_safely(tool_executor, name, arguments, round_num=None):
    """Call the caller's tool_executor and return whatever it returns
    (usually a dict) — or a small error dict if it raised. Never lets a
    broken tool take down the request loop.

    Phase 0 (see new_plan.md): providers never report token counts for an
    individual tool call/result on their own — only for the whole
    request/response it's embedded in — so input_tokens/output_tokens
    here are a local ~estimate (token_usage.estimate_tokens_for), logged
    right alongside the call so the debug panel and Logs viewer can show
    "how many tokens did this tool call cost" per call, not just per ask.
    """
    conv_id = _log_conv_id()
    name = _normalize_tool_name(name)
    input_tokens = token_usage.estimate_tokens_for(arguments)
    try:
        result = tool_executor(name, arguments)
    except Exception as e:
        result = {"error": f"{name} failed: {e}"}

    # ai_client._make_tool_executor shares one tool_executor (and its
    # result cache) across every provider/key failover in a single ask()
    # specifically so a retried request never re-runs the same tool call —
    # it marks each call as a cache hit or a real run via this attribute.
    # A cache hit didn't cost anything new (no tool actually executed, no
    # extra bytes sent to any provider), so it must not be logged or
    # counted again here — otherwise every failover retry re-inflates the
    # token trace and get_usage_summary()'s tool_calls list with duplicate
    # entries for work that didn't happen. Executors that don't set this
    # (e.g. no-cache callers, tests) default to "not a cache hit" so
    # behavior for them is unchanged.
    if getattr(tool_executor, "_cache_hit", False):
        return result

    if conv_id:
        logs.log(
            conv_id, "tool_call",
            {"name": name, "arguments": arguments, "input_tokens": input_tokens},
            provider=_log_provider(), round_num=round_num,
        )
    output_tokens = token_usage.estimate_tokens_for(result)
    if conv_id:
        logs.log(
            conv_id, "tool_result",
            {"name": name, "result": result, "output_tokens": output_tokens},
            provider=_log_provider(), round_num=round_num,
        )
    tool_usage = getattr(_log_local, "tool_usage", None)
    if tool_usage is None:
        tool_usage = []
        _log_local.tool_usage = tool_usage
    tool_usage.append({
        "name": name,
        "round": round_num,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "source": "estimated",
    })
    on_usage = getattr(_log_local, "on_tool_usage", None)
    if on_usage:
        try:
            on_usage(name, input_tokens, output_tokens)
        except Exception:
            pass  # a broken display callback must never break the actual tool call
    return result


MAX_TOOL_RESULT_CHARS = 4000


def _stringify_tool_result(result):
    """Most providers want the tool result as a plain string. Structured
    (dict/list) results get JSON-encoded so the model can still read the
    individual fields precisely rather than a mangled repr()."""
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result)
        except TypeError:
            text = str(result)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[: MAX_TOOL_RESULT_CHARS - 14].rstrip() + "…[truncated]"


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
# Forced ending (master plan F.1 + F.8).
#
# THE PROBLEM. Every adapter withholds `tools` once the round budget is spent
# (or MAX_TOOL_ROUNDS is reached) but keeps sending a transcript that is FULL
# of native tool-call structures. A model cannot un-learn a call it can see
# itself making, so it kept making one: Gemini answered with a functionCall
# (or a MALFORMED_FUNCTION_CALL), Groq with the call written into its
# `reasoning` channel or a 400, Ollama with prose. Every adapter treated that
# as a dead key and rotated. In the 2026-09-20 logs the discarded call was, in
# one case, exactly the Move-Item command the user wanted run — and ten keys
# were spent finding that out.
#
# THE SHAPE OF THE FIX. When tools have to go, the adapter hands the rest of
# the ask to _forced_ending() instead of sending one more structured request:
#
#   1. GRACE ROUND (once per ask, only if RoundBudget(grace=True)). The whole
#      transcript so far is flattened to plain text, the tools come BACK, and
#      the model is told it may make one more call if it is essential. Any call
#      it makes goes through the ordinary tool_executor — i.e. through the
#      normal confirm gate — never around it.
#   2. FINAL ANSWER. The transcript, now including the grace call's result, is
#      flattened again and sent with NO tools and no structure to imitate, plus
#      one shared notice that tools are gone. Tool calls and results are prose
#      ("I ran X with {...}. Result of X: ..."), so there is nothing to copy.
#
# If the final request still comes back empty or containing a call, that is not
# a key fault: the adapter returns KIND_BUDGET with the call it was about to
# make, and ask() ends the turn through a harness-written reply instead of
# rotating (F.11).
#
# WHY RECURSION INTO THE SAME ADAPTER. Flat text is the generic message format
# every adapter already accepts at its front door, so re-entering
# `call_x(provider, flat_messages, tools=None|tools, tool_executor=None)` reuses
# 100% of each adapter's request building, error handling, usage and logging —
# instead of five hand-written "finish up" requests that would drift apart.
# The cost is that a re-entered adapter sees round 0; _log_local.forced marks
# that state so thinking stays off and usage rounds keep their real numbers.
# ---------------------------------------------------------------------------

GRACE_MAX_CALLS = 4  # calls honoured from the single grace response (a normal round runs all of a response's calls too)

# gpt-oss models are trained on tool namespaces the provider doesn't strip:
# a call to `list_dir` can arrive as `repo_browser.list_dir` (seen in the Groq
# failed_generation of Case 1) or `functions.list_dir`.
_TOOL_NAME_PREFIXES = ("repo_browser.", "functions.")


def _normalize_tool_name(name):
    if isinstance(name, str):
        for prefix in _TOOL_NAME_PREFIXES:
            if name.startswith(prefix):
                return name[len(prefix):]
    return name


def _capture_pending(calls):
    """Adapters call this wherever they have just parsed the model's tool
    calls. A no-op unless a forced ending is collecting them — that is how the
    wrapper learns what the model WANTED to call without the adapter executing
    it (tool_executor is None in those re-entered calls)."""
    forced = getattr(_log_local, "forced", None)
    if forced is None or not calls:
        return
    for name, args in calls:
        forced["captured"].append((_normalize_tool_name(name), args if isinstance(args, dict) else {}))


def _forced_active():
    return getattr(_log_local, "forced", None) is not None


def _openai_style_calls(tool_calls):
    """[(name, args)] from an OpenAI/Cohere/Ollama-shaped `tool_calls` list."""
    out = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        out.append((fn.get("name", ""), _decode_arguments(fn.get("arguments"))))
    return out


_HARMONY_CALL_RE = re.compile(r"to=(?:functions|repo_browser)\.([A-Za-z0-9_]+)[^{]*?(\{.*\})", re.S)


def _calls_from_reasoning(message):
    """A call the model wrote into its reasoning channel instead of tool_calls
    (Groq/gpt-oss, Case 1: `content: ""`, finish_reason "stop", the call as JSON
    in `reasoning`). Only consulted when the visible reply is empty. Returns at
    most the LAST call found — earlier ones in a reasoning trace are drafts.

    The two shapes handled are educated guesses from the plan's description
    ("written as JSON inside reasoning") plus gpt-oss's own harmony syntax; the
    raw log wasn't available, so both are covered by tests on synthetic samples
    only. A wrong guess is harmless: the wrapper only honours names that are in
    the offered tool list."""
    if not isinstance(message, dict):
        return []
    text = ""
    for key in ("reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            text = value
            break
    if not text:
        return []
    calls = []
    m = _HARMONY_CALL_RE.search(text)
    if m:
        obj, _ = _json_object_at(m.group(2), 0)
        if isinstance(obj, dict):
            calls.append((m.group(1), obj))
    i = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            break
        obj, end = _json_object_at(text, i)
        if obj is None:
            i += 1
            continue
        i = max(end, i + 1)
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            continue
        for key in ("arguments", "args", "parameters", "input"):
            if key in obj:
                args = obj[key]
                if isinstance(args, str):
                    args = _decode_arguments(args)
                if isinstance(args, dict):
                    calls.append((obj["name"], args))
                break
    return calls[-1:]


def _pending_from_text(text, allowed):
    """Calls a model wrote as plain text (`[called x with {...}]` or a bare
    {"name":..., "arguments":...} object) — but only when that IS the reply,
    not a real answer that happens to mention one."""
    calls = _extract_text_tool_calls(text or "", allowed)
    if not calls:
        return []
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        return calls
    prose = re.sub(r"\[called\s+[A-Za-z0-9_.\-]+\s+with\s+.*?\]", "", stripped, flags=re.S)
    return calls if len(prose.strip(" \n:-")) < 40 else []


# The bracketed "[called x with {...}]" / "[tool result] ..." lines are how
# every _*_to_generic() serialises a round trip so the NEXT provider can read
# it. They are also exactly the syntax _extract_text_tool_calls parses and
# _is_tool_trace_reply rejects, i.e. a model that copies them produces a fake
# call, not an answer. For the final request they are rewritten as prose.
_GENERIC_CALL_LINE = re.compile(r"\[called\s+(\S+)\s+with\s+(.*)\]\s*$", re.M)
_GENERIC_RESULT_FOR = re.compile(r"^\[tool result for ([^\]]+)\]\s*", re.M)
_GENERIC_RESULT = re.compile(r"^\[tool result\]\s*", re.M)


def _flatten_tool_scaffold(generic):
    """A copy of `generic` (a list of {role, content}) in which every recorded
    tool call/result reads as ordinary prose. Roles, order and everything that
    isn't scaffolding are left exactly as they were."""
    out = []
    for m in generic or []:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, str):
            out.append(m)
            continue
        role = m.get("role")
        if role == "assistant" and "[called " in content:
            content = _GENERIC_CALL_LINE.sub(r"I ran \1 with \2.", content)
        elif role == "user" and content.lstrip().startswith("[tool result"):
            content = _GENERIC_RESULT_FOR.sub(lambda mo: f"Result of {mo.group(1)}: ", content)
            content = _GENERIC_RESULT.sub("Result: ", content)
        out.append({**m, "content": content})
    return out


def _with_notice(generic, notice):
    """`generic` plus `notice` as the final user turn. Merged into the last
    message when that is already a user turn, so providers that dislike two
    consecutive user messages never see them."""
    out = list(generic)
    if out and out[-1].get("role") == "user" and isinstance(out[-1].get("content"), str):
        out[-1] = {**out[-1], "content": out[-1]["content"] + "\n\n" + notice}
    else:
        out.append({"role": "user", "content": notice})
    return out


def _tools_withheld(round_num, round_budget):
    return round_num >= MAX_TOOL_ROUNDS or round_budget.remaining() <= 0


def _forced_ending_due(round_num, round_budget, ran_tools, tool_executor):
    """True when this round must not offer the tools it normally would AND
    there is something a forced ending can do about it: a transcript full of
    native tool structure to flatten, or a grace call left to run. A first
    round with plain history, a spent budget and no grace to give keeps the
    plain (notice-only) path — nothing to flatten, nothing to run."""
    if not _tools_withheld(round_num, round_budget):
        return False
    return bool(ran_tools) or (tool_executor is not None and round_budget.grace_available())


def _record_pair(history, name, args, result):
    try:
        args_s = json.dumps(args, default=str)
    except (TypeError, ValueError):
        args_s = str(args)
    history.append({"role": "assistant", "content": f"I ran {name} with {args_s}."})
    history.append({"role": "user", "content": f"Result of {name}: {_stringify_tool_result(result)}"})


def _forced_ending(adapter, provider, generic, timeout, tools, tool_executor, round_budget,
                   cfg_defaults, round_num):
    """Finish an ask() whose tools must be withheld. See the block comment
    above for the design. Returns an AIResult; never raises."""
    allowed = {t.get("name") for t in (tools or []) if isinstance(t, dict) and t.get("name")}
    previous = getattr(_log_local, "forced", None)
    state = {"allowed": allowed, "captured": [], "round_base": round_num}
    _log_local.forced = state
    try:
        history = _flatten_tool_scaffold(generic)

        def _wanted(result):
            """Calls the model wanted: captured natively, written into its
            reasoning, or written as its whole reply. Only offered tool names
            count; the rest is noise."""
            calls = list(state["captured"])
            if result is not None and result.ok:
                calls += _pending_from_text(result.text, allowed)
            if result is not None and result.pending:
                calls += list(result.pending)
            seen, kept = set(), []
            for name, args in calls:
                name = _normalize_tool_name(name)
                key = (name, json.dumps(args, sort_keys=True, default=str))
                if name in allowed and key not in seen:
                    seen.add(key)
                    kept.append((name, args))
            return kept

        # 1. Grace round — tools come back, once, one more time.
        if tool_executor is not None and round_budget.take_grace():
            state["captured"] = []
            r = adapter(provider, _with_notice(history, _GRACE_NOTICE), timeout, tools=tools,
                        tool_executor=None, round_budget=RoundBudget(), cfg_defaults=cfg_defaults)
            calls = _wanted(r)[:GRACE_MAX_CALLS]
            if not calls:
                if r.ok:
                    return r  # it simply answered
                if r.kind not in (KIND_EMPTY, KIND_MALFORMED, KIND_BUDGET):
                    # A real failure (key, network, shape): let ask() handle it
                    # the normal way rather than spending a second request here.
                    return AIResult(False, error=r.error, tool_history=history, kind=r.kind)
            for name, args in calls:
                _record_pair(history, name, args, _call_tool_safely(tool_executor, name, args, round_num))
            state["round_base"] = round_num + 1

        # 2. Final answer — no tools, nothing structural to imitate.
        state["captured"] = []
        r = adapter(provider, _with_notice(history, _TOOLS_WITHHELD_NOTICE), timeout, tools=None,
                    tool_executor=None, round_budget=RoundBudget(), cfg_defaults=cfg_defaults)
        wanted = _wanted(r)
        if r.ok and not wanted:
            return r
        if wanted or r.kind in (KIND_EMPTY, KIND_MALFORMED, KIND_BUDGET):
            # Still wants a tool, or produced nothing usable, even with the
            # structure gone: end it here — another key would do the same.
            return AIResult(False, error=_give_up_error(), tool_history=history,
                            kind=KIND_BUDGET, pending=wanted)
        return AIResult(False, error=r.error, tool_history=history, kind=r.kind)
    except Exception as e:  # noqa: BLE001 — a forced ending must never take the ask down with it
        return AIResult(False, error=f"unexpected error while finishing the reply: {e}")
    finally:
        _log_local.forced = previous



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

_TOOLS_WITHHELD_NOTICE = (
    "You can't use tools for the rest of this reply. Do not call any tool. Answer the "
    "user now, in plain text, from the results already shown above. If they don't fully "
    "finish the job, say plainly what was done and what still remains, in the user's own "
    "terms. Never mention tools, limits, budgets or rounds."
)
# The one grace round (see _forced_ending): tools are back, once.
_GRACE_NOTICE = (
    "You may make ONE more tool call, and only if it is essential to finish what the user "
    "asked. If you can already answer, answer in plain text now. Never mention tools, "
    "limits, budgets or rounds to the user."
)


def _looks_like_omitted_tools_confused_the_model(reason):
    """Groq's tool-calling validation is stricter than the rest of the
    openai_compatible family: if `tools` is left out of the request
    entirely (jarvis's forced-final-round behavior — see the round_num <
    MAX_TOOL_ROUNDS gate above) but the model still emits a tool call
    anyway, Groq 400s instead of just treating it as ignorable text. Two
    message shapes seen in practice for the same underlying mismatch:
    "Tool choice is none, but model called a tool" and "attempted to call
    tool 'X' which was not in request.tools". Matched loosely (substring,
    not an exact string) since the exact wording isn't documented/stable
    API contract, just an observed pattern — a false negative here just
    means the normal error path handles it as before.
    """
    if not reason:
        return False
    lowered = reason.lower()
    return (
        ("tool choice is none" in lowered and "called a tool" in lowered)
        or "which was not in request.tools" in lowered
        or ("tool_choice" in lowered and "none" in lowered and "tool" in lowered)
    )


def call_openai_compatible(provider, messages, timeout, tools=None, tool_executor=None, round_budget=None,
                           cfg_defaults=None):
    base_url = provider.get("base_url") or ""
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not base_url:
        return AIResult(False, error="no base_url configured")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # _merge_system: ai_client emits the system prompt as two messages
    # (static prefix + per-request tail) so call_anthropic can put a cache
    # breakpoint between them. Every other provider gets them folded back
    # into the single system message it has always received — byte-identical
    # to the pre-caching payload, since both sides join with "\n\n".
    working_messages = _merge_system(messages)
    ran_tools = False
    thought_rounds = 0
    if round_budget is None:
        round_budget = RoundBudget()
    # See provider_wants_nullable_optionals: Groq validates tool arguments
    # server-side, so an optional parameter the model sends as null has to be
    # declared nullable or the whole call 400s before jarvis sees it.
    widen_optionals = provider_wants_nullable_optionals(provider)

    def _tools_payload():
        # Phase 9 of the token-optimization plan (see new_plan.md):
        # rebuilt every round instead of once before the loop, so a
        # Phase 5 search_tools match (which grows the *same* `tools` list
        # object the caller passed in, see ai_client._make_tool_executor's
        # discover_sink) is actually advertised starting the very next
        # round instead of only existing in that round's tool reply.
        if not tools:
            return None
        return [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": (nullable_optional_parameters(t["parameters"])
                               if widen_optionals else t["parameters"]),
            }}
            for t in tools
        ]

    system_parts_in, _ = _system_parts(messages)
    cache_plan = prompt_cache.plan(
        system_parts_in, _tools_payload(), provider, cfg_defaults or {}, model=model,
    )
    _log_cache_plan(cache_plan, "openai_compatible")

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        # Trim tool results from earlier rounds before rebuilding this
        # round's payload — see _compact_prior_tool_results. Without this,
        # round N resends rounds 1..N-1's results at full size every time.
        _compact_prior_tool_results(working_messages)
        # F.1/F.8: tools must be withheld now. Rather than send one more
        # request whose transcript is full of calls the model will keep
        # trying to make, hand the ending to _forced_ending (grace round, then
        # a flattened, tool-less final answer).
        if tools and _forced_ending_due(round_num, round_budget, ran_tools, tool_executor):
            return _forced_ending(call_openai_compatible, provider, _openai_messages_to_generic(working_messages),
                                  timeout, tools, tool_executor, round_budget, cfg_defaults, round_num)
        tools_payload = _tools_payload()
        tools_omitted_this_round = not (tools_payload and round_num < MAX_TOOL_ROUNDS and round_budget.remaining() > 0)
        # When tools exist but are being WITHHELD (round cap or the shared
        # cross-key budget is spent), the transcript still shows the model
        # calling tools, and it keeps trying. On Groq that is the 400 "Tool
        # choice is none, but model called a tool"; elsewhere it is prose like
        # "I will now search for ...". Say so explicitly instead. Sent for this
        # request only — never appended to working_messages, so it can't leak
        # into the transcript handed to the next provider on failover.
        request_messages = working_messages
        if tools_omitted_this_round and tools_payload:
            request_messages = working_messages + [{"role": "user", "content": _TOOLS_WITHHELD_NOTICE}]
        payload = {
            "model": model,
            "messages": request_messages,
            "max_tokens": provider.get("max_tokens", 700),
        }
        # Prompt caching for the whole OpenAI-compatible family (Groq,
        # OpenAI, xAI, Mistral, DeepSeek, OpenRouter). These hosts cache
        # prefixes AUTOMATICALLY above ~1024 tokens, with no opt-in and no
        # extra fee, so there is no marker to place — the actual work was
        # already done upstream by ai_client putting the static system block
        # first and the per-request tail last.
        #
        # The one request-side lever is prompt_cache_key: a stable string
        # routing this request to the same backend node the last one hit,
        # which is what turns a possible hit into a likely one. It is a hint,
        # never a guarantee. Keyed on the conversation, so every turn of one
        # chat lands together, and deliberately not on anything per-request.
        #
        # NOT every host in this family tolerates an unrecognized field:
        # Groq's endpoint 400s outright on prompt_cache_key ("property
        # 'prompt_cache_key' is unsupported"), on every request, every key —
        # not a fluke. prompt_cache.resolve_settings() already defaults
        # send_cache_key to False for names in NO_CACHE_KEY_PROVIDER_NAMES,
        # so cache_plan["cache_key"] is already False for Groq by the time
        # it reaches here; this comment is the "why" for that default, not
        # something this call site needs to re-check. "prompt_cache_key":
        # true in a Groq provider block would still turn it back on and
        # promptly fail again — that's an explicit choice, not a bug.
        if cache_plan.get("cache_key"):
            cache_key = _log_conv_id()
            if cache_key:
                payload["prompt_cache_key"] = str(cache_key)
        if not tools_omitted_this_round:
            payload["tools"] = tools_payload
        if _apply_thinking(payload, provider, "openai_compatible", round_num,
                           ran_tools, thought_rounds):
            thought_rounds += 1
        extra = provider.get("extra_params")
        if isinstance(extra, dict):
            # extra_params last, so an explicit per-provider override always
            # beats what the thinking level asked for.
            payload.update(extra)

        resp, net_err = _post_json(base_url, headers, payload, timeout)
        # One retry without the thinking keys if THAT is what was rejected.
        # Some OpenAI-compatible hosts 400 on an unknown field (Groq does
        # exactly this for prompt_cache_key), and burning a whole API key
        # over a request shape jarvis chose — not something the user did —
        # is the failure this avoids.
        if not net_err:
            _reason = _status_reason(resp)
            if _reason:
                from . import reasoning as _r
                if _r.looks_like_thinking_rejected(_reason) and _r.strip_from_payload(payload):
                    resp, net_err = _post_json(base_url, headers, payload, timeout)
        if net_err:
            return AIResult(False, error=net_err,
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
        reason = _status_reason(resp)
        if reason and tools_omitted_this_round and tools_payload \
                and _looks_like_omitted_tools_confused_the_model(reason):
            # See _looks_like_omitted_tools_confused_the_model's docstring.
            # Retry this SAME round once, with tools included after all —
            # bypassing the forced-final-round omission just for this one
            # request — rather than burning the whole key over a request
            # shape jarvis chose, not something the caller did wrong. Only
            # ever one retry per round (this isn't inside a loop that could
            # re-trigger it), so a provider that fails this way for some
            # other, unrelated reason still terminates normally.
            payload["tools"] = tools_payload
            # Tools are attached again, so "do not call tools" would now
            # contradict the request.
            payload["messages"] = working_messages
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
        # Called exactly once per round. This line was duplicated, which
        # double-counted every openai-compatible round in the usage
        # summary, the debug panel and the Logs viewer. Found while adding
        # the cache-token accounting above, which would have inherited the
        # same doubling.
        _record_usage("openai_compatible", data, round_num)
        _collect_thinking("openai_compatible", data)

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
        _capture_pending(_openai_style_calls(tool_calls))

        if tool_calls and tool_executor and round_num < MAX_TOOL_ROUNDS and round_budget.take():
            ran_tools = True
            working_messages.append(message)
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = _decode_arguments(fn.get("arguments"))
                result_text = _stringify_tool_result(_call_tool_safely(tool_executor, name, args, round_num))
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
                return AIResult(False, error=_give_up_error(), pending=_openai_style_calls(tool_calls),
                                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
            if _forced_active():
                # gpt-oss on Groq writes its call into `reasoning` and leaves
                # `content` empty (Case 1). Record it so the forced ending
                # knows the model wanted a tool; the reply is still "empty".
                _capture_pending(_calls_from_reasoning(message))
            return AIResult(False, error="empty response content",
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)

        allowed = {t.get("name") for t in (tools or []) if t.get("name")}
        text_calls = _extract_text_tool_calls(text, allowed)
        if text_calls and tool_executor and round_num < MAX_TOOL_ROUNDS and round_budget.take():
            ran_tools = True
            result_bits = []
            for name, args in text_calls:
                raw = _call_tool_safely(tool_executor, name, args, round_num)
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

        return AIResult(True, text=text, usage=get_usage_summary())

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

def call_anthropic(provider, messages, timeout, tools=None, tool_executor=None, round_budget=None,
                   cfg_defaults=None):
    base_url = provider.get("base_url") or "https://api.anthropic.com/v1/messages"
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not api_key:
        return AIResult(False, error="no api_key configured")

    system_blocks_in, turns = _system_parts(messages)
    system_text = "\n\n".join(system_blocks_in)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    working_turns = list(turns)
    ran_tools = False
    thought_rounds = 0
    if round_budget is None:
        round_budget = RoundBudget()
    cache_defaults = cfg_defaults or {}

    def _tools_payload():
        # Phase 9 (see new_plan.md): rebuilt every round so a Phase 5
        # search_tools match, appended to the same `tools` list object by
        # ai_client._make_tool_executor's discover_sink, is offered
        # starting next round.
        if not tools:
            return None
        return [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]

    def _history():
        return _anthropic_turns_to_generic(system_text, working_turns) if ran_tools else None

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        # Trim tool results from earlier rounds before rebuilding this
        # round's payload — see _compact_prior_tool_results. Without this,
        # round N resends rounds 1..N-1's results at full size every time.
        _compact_prior_tool_results(working_turns)
        if tools and _forced_ending_due(round_num, round_budget, ran_tools, tool_executor):
            # Keep the system blocks separate (not the single joined string
            # _history() builds) so the final request keeps the same cache
            # breakpoint layout as every round before it.
            generic = ([{"role": "system", "content": part} for part in system_blocks_in]
                       + _anthropic_turns_to_generic("", working_turns))
            return _forced_ending(call_anthropic, provider, generic, timeout, tools, tool_executor,
                                  round_budget, cfg_defaults, round_num)
        tools_payload = _tools_payload()
        payload = {
            "model": model,
            "max_tokens": provider.get("max_tokens", 700),
            "messages": working_turns,
        }
        offering_tools = bool(tools_payload) and round_num < MAX_TOOL_ROUNDS and round_budget.remaining() > 0
        if tools_payload and not offering_tools:
            # F.1: tools are withheld and the model is told so, not left to
            # find out by trying. Request-only; never added to working_turns.
            payload["messages"] = working_turns + [{"role": "user", "content": _TOOLS_WITHHELD_NOTICE}]
        if _apply_thinking(payload, provider, "anthropic", round_num,
                           ran_tools, thought_rounds):
            thought_rounds += 1
        # Prompt caching (lever 3 of the token-optimization research; see
        # prompt_cache.py). Re-planned every round rather than once before
        # the loop, because the thing it sizes against — the tools array —
        # is itself rebuilt every round and can grow mid-ask when
        # search_tools/get_tool_schema promote a tool.
        plan = prompt_cache.plan(
            system_blocks_in,
            tools_payload if offering_tools else None,
            provider,
            cache_defaults,
            model=model,
        )
        if system_text:
            if plan["system_index"] is None:
                # No breakpoint earned: send the plain string, which is the
                # exact payload this adapter built before caching existed.
                payload["system"] = system_text
            else:
                payload["system"] = prompt_cache.apply_to_system(
                    system_blocks_in, plan["system_index"], plan["ttl"],
                )
        if offering_tools:
            payload["tools"] = (
                prompt_cache.apply_to_tools(tools_payload, plan["ttl"])
                if plan["tools"] else tools_payload
            )
        if round_num == 0:
            _log_cache_plan(plan, "anthropic")

        resp, net_err = _post_json(base_url, headers, payload, timeout)
        if net_err:
            return AIResult(False, error=net_err, tool_history=_history())
        reason = _status_reason(resp)
        if reason:
            from . import reasoning as _r
            if _r.looks_like_thinking_rejected(reason) and _r.strip_from_payload(payload):
                resp, net_err = _post_json(base_url, headers, payload, timeout)
                if net_err:
                    return AIResult(False, error=net_err, tool_history=_history())
                reason = _status_reason(resp)
            if reason:
                return AIResult(False, error=reason, tool_history=_history())

        data, parse_err = _parse_json(resp)
        if parse_err:
            return AIResult(False, error=parse_err, tool_history=_history())

        _record_usage("anthropic", data, round_num)
        _collect_thinking("anthropic", data)

        if data.get("stop_reason") == "refusal":
            return AIResult(False, error="refused by the model's safety classifier", tool_history=_history())

        blocks = data.get("content") or []
        tool_use_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        _capture_pending([(b.get("name", ""), b.get("input") or {}) for b in tool_use_blocks])

        if tool_use_blocks and tool_executor and round_num < MAX_TOOL_ROUNDS and round_budget.take():
            ran_tools = True
            # `blocks` is appended whole, thinking blocks included. That is
            # required, not incidental: Anthropic rejects a replayed
            # tool_use turn whose thinking blocks (and their signatures) are
            # missing, so stripping them here to save input tokens would
            # break every multi-round turn with thinking on. This is the one
            # place a trace is legitimately resent — see reasoning.carry_blocks.
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
                return AIResult(False, error=_give_up_error(), tool_history=_history(),
                                pending=[(b.get("name", ""), b.get("input") or {}) for b in tool_use_blocks])
            return AIResult(False, error="empty response content", tool_history=_history())
        return AIResult(True, text=text, usage=get_usage_summary())

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


_GEMINI_CACHE_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Gemini's explicit context-caching API (cachedContents) has a real minimum
# size the cached content must meet before the provider will accept it at
# all — well documented as being in the low thousands of tokens. Jarvis's
# own system prompt + tool schemas typically run ~900-1,600 tokens (see the
# benchmark), comfortably under that floor. Attempting to cache content
# this small doesn't just fail harmlessly — it costs a full extra network
# round trip (create attempt, every single ask) for a guaranteed rejection,
# which is pure added latency with zero token savings. Guard against that
# with a cheap pre-check using the same ~4-chars/token heuristic
# token_usage.py already uses elsewhere, rather than paying for a doomed
# API call to find out. This threshold is deliberately conservative (below
# the documented minimum for every model Jarvis currently targets) — if a
# future provider/model lowers its minimum, this just means caching stays
# off for content that might have technically qualified, never a
# correctness problem.
_GEMINI_CACHE_MIN_TOKENS = 4096


def _gemini_cache_name(model, headers, system_text, tools_payload, timeout, ttl_seconds):
    """Get a reusable cachedContents name for this prefix, creating one only
    if no live cache already covers it.

    This is the fix for the "janky" half of the old implementation. That
    version created a cache at the start of every ask and DELETED it at the
    end of the same ask, which meant each fresh jarvis process paid:

      - an extra HTTP round trip before the first generateContent, on the
        latency path
      - cache-creation billing at the standard input-token rate
      - storage billing for however long it lived

    ...to collect a discount only on rounds 2+ of that one ask. Most asks are
    a single round, so that was usually a net loss, and it could never be
    anything else, because the cache was destroyed before the next process
    could reach it.

    Now the name is fingerprinted by exactly what went into it and persisted
    (gemini_cache_store.py), so process N+1 reuses process N's cache and the
    write is amortized across every ask that shares the prefix. The cache is
    left to expire on its TTL instead of being deleted, which is the only
    arrangement where explicit caching beats Gemini's free implicit caching.

    Best-effort throughout, same philosophy as discovery_cache.py: a rejected
    cache, a network error, or a malformed response all collapse to None and
    the caller sends content inline exactly as before. Never raises.
    """
    from . import gemini_cache_store

    if not system_text and not tools_payload:
        return None

    key = gemini_cache_store.fingerprint(model, system_text, tools_payload)
    existing = gemini_cache_store.lookup(key)
    if existing:
        return existing

    body = {
        "model": f"models/{model}",
        "ttl": f"{int(ttl_seconds)}s",
    }
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    if tools_payload:
        body["tools"] = tools_payload
    resp, net_err = _post_json(f"{_GEMINI_CACHE_BASE}/cachedContents", headers, body, timeout)
    if net_err:
        return None
    if not (200 <= resp.status_code < 300):
        return None
    data, parse_err = _parse_json(resp)
    if parse_err or not isinstance(data, dict):
        return None
    name = data.get("name")
    if name:
        gemini_cache_store.store(key, name, ttl_seconds)
    return name


def _delete_gemini_cache(cache_name, headers, timeout):
    """Drop a cache we know is unusable.

    No longer called at the end of every ask — that was the bug. It's called
    only when a cache has been invalidated for real: the tool list grew
    mid-ask, so the cached schemas no longer match what the model needs to be
    offered. Deleting it then is correct; deleting it on the happy path threw
    away the whole point.
    """
    if not cache_name:
        return
    from . import gemini_cache_store

    gemini_cache_store.forget(cache_name)
    try:
        requests.delete(f"{_GEMINI_CACHE_BASE}/{cache_name}", headers=headers, timeout=timeout)
    except requests.exceptions.RequestException:
        pass


# gemini-2.5+ models "think" by default — the model spends output tokens on
# an invisible reasoning trace BEFORE any visible answer text, and
# maxOutputTokens caps the two together, not separately. The blind fallback
# of 700 used everywhere else in this file was sized for plain completions
# and is nowhere near enough for a thinking model: a real, reproducible
# failure mode is every single round finishing with finishReason=MAX_TOKENS
# and zero visible text, because the whole budget went to the hidden trace.
# Unlike a rate limit or a bad key, this reproduces identically on every
# key against the same model/config — which is exactly the "empty response
# content" x10 signature this constant exists to stop being the default.
# An explicit "max_tokens" in the provider block always overrides this.
GEMINI_DEFAULT_MAX_TOKENS = 3072


def call_gemini(provider, messages, timeout, tools=None, tool_executor=None, round_budget=None,
                cfg_defaults=None):
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not api_key:
        return AIResult(False, error="no api_key configured")

    base_url = provider.get("base_url") or (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )
    url = base_url.format(model=model) if "{model}" in base_url else base_url

    system_parts_in, turns = _system_parts(messages)
    system_text = "\n\n".join(system_parts_in)

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
    thought_rounds = 0
    if round_budget is None:
        round_budget = RoundBudget()

    def _tools_payload():
        # Phase 9 (see new_plan.md): rebuilt every round so a Phase 5
        # search_tools match, appended to the same `tools` list object by
        # ai_client._make_tool_executor's discover_sink, is offered
        # starting next round.
        if not tools:
            return None
        return [{"function_declarations": [
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

    # Gemini offers two caching mechanisms, and for jarvis the free one is
    # usually the better one:
    #
    #   IMPLICIT (the default here) — on by default for 2.5+ models, costs
    #     nothing, needs no extra request, and simply requires a stable
    #     prefix. ai_client already guarantees that by putting the static
    #     system block first and the query-dependent tail last, so there is
    #     literally nothing to do at this layer but not break it. That's why
    #     systemInstruction is built from system_text (static block first)
    #     rather than being reassembled in some other order per round.
    #
    #   EXPLICIT (opt-in: "gemini_explicit_cache": true) — the cachedContents
    #     API. Guarantees the discount, but costs a POST before the first
    #     generateContent plus storage billing for the TTL. Worth it only
    #     when the same prefix is reused enough to amortize the write, which
    #     is exactly what _gemini_cache_name's cross-process store now makes
    #     possible and the old delete-at-end-of-ask code made impossible.
    #
    # Either way, cache_name None means inline-every-round — the safe path.
    initial_tools_payload = _tools_payload()
    cache_plan = prompt_cache.plan(
        system_parts_in, initial_tools_payload, provider, cfg_defaults or {}, model=model,
    )
    _log_cache_plan(cache_plan, "gemini")
    cache_name = None
    if cache_plan.get("eligible") and cache_plan.get("explicit"):
        cache_name = _gemini_cache_name(
            model, headers, system_text, initial_tools_payload, timeout,
            prompt_cache.ttl_seconds(cache_plan.get("ttl")),
        )
    cached_tool_count = len(tools) if (cache_name and tools) else 0

    try:
        for round_num in range(MAX_TOOL_ROUNDS + 1):
            # Trim tool results from earlier rounds before rebuilding this
            # round's payload — see _compact_prior_tool_results. Without this,
            # round N resends rounds 1..N-1's results at full size every time.
            _compact_prior_tool_results(working_contents)
            if tools and _forced_ending_due(round_num, round_budget, ran_tools, tool_executor):
                generic = ([{"role": "system", "content": part} for part in system_parts_in]
                           + _gemini_contents_to_generic(working_contents))
                return _forced_ending(call_gemini, provider, generic, timeout, tools, tool_executor,
                                      round_budget, cfg_defaults, round_num)
            tools_payload = _tools_payload()

            # `tools` can grow mid-ask (ai_client's discover_sink appends to
            # the same list object once a search_tools/search_commands hit
            # lands), which would make an already-created cache stale — the
            # model would never be offered the newly-discovered tool. If
            # that's happened, drop the stale cache and fall back to
            # sending the (now-larger) schema inline for the rest of the ask
            # rather than risk the model missing the discovered tool.
            if cache_name and tools is not None and len(tools) != cached_tool_count:
                _delete_gemini_cache(cache_name, headers, timeout)
                cache_name = None

            payload = {
                "contents": working_contents,
                "generationConfig": {"maxOutputTokens": provider.get("max_tokens", GEMINI_DEFAULT_MAX_TOKENS)},
            }
            if _apply_thinking(payload, provider, "gemini", round_num,
                               ran_tools, thought_rounds):
                thought_rounds += 1
            extra = provider.get("extra_params")
            if isinstance(extra, dict):
                # Merged into generationConfig specifically (not the top
                # level payload, unlike the openai_compatible family) — this
                # is where thinkingConfig, topP, topK etc. actually live in
                # Gemini's request shape. Lets a provider block turn off
                # reasoning traces entirely with
                # "extra_params": {"thinkingConfig": {"thinkingBudget": 0}}
                # instead of just raising max_tokens to outrun them.
                payload["generationConfig"].update(extra)
            if cache_name:
                payload["cachedContent"] = cache_name
            else:
                if system_text:
                    payload["systemInstruction"] = {"parts": [{"text": system_text}]}
                if tools_payload and round_num < MAX_TOOL_ROUNDS and round_budget.remaining() > 0:
                    payload["tools"] = tools_payload
                elif tools_payload:
                    # F.1: withheld and the model is told so. Request-only.
                    payload["contents"] = working_contents + [
                        {"role": "user", "parts": [{"text": _TOOLS_WITHHELD_NOTICE}]}]

            resp, net_err = _post_json(url, headers, payload, timeout)
            if net_err:
                return AIResult(False, error=net_err, tool_history=_history())
            reason = _status_reason(resp)
            if reason:
                return AIResult(False, error=reason, tool_history=_history())

            data, parse_err = _parse_json(resp)
            if parse_err:
                return AIResult(False, error=parse_err, tool_history=_history())

            _record_usage("gemini", data, round_num)
            _collect_thinking("gemini", data)

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
            _capture_pending([(p["functionCall"].get("name", ""), p["functionCall"].get("args") or {})
                              for p in call_parts])

            if call_parts and tool_executor and round_num < MAX_TOOL_ROUNDS and round_budget.take():
                ran_tools = True
                # Thought parts are stripped before the model turn is echoed
                # back. Gemini rejects a replayed turn that still contains
                # them, so with includeThoughts on this would fail EVERY
                # multi-round turn — and it's the opposite of Anthropic,
                # which requires them kept. Same field, inverted rule; the
                # difference is why reasoning.carry_blocks() is per-provider.
                working_contents.append({
                    "role": "model",
                    "parts": [p for p in parts
                              if not (isinstance(p, dict) and p.get("thought"))],
                })
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

            # Thought parts excluded: with includeThoughts on, Gemini returns
            # the reasoning as ordinary text parts flagged `thought: true`.
            # Joining them all would print the model's private deliberation
            # to the user as if it were the answer.
            text = "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict) and not p.get("thought")).strip()
            if not text:
                if call_parts:
                    return AIResult(False, error=_give_up_error(), tool_history=_history(),
                                    pending=[(p["functionCall"].get("name", ""), p["functionCall"].get("args") or {})
                                             for p in call_parts])
                if finish_reason in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"):
                    # The model tried to call a tool and garbled it. This used
                    # to be reported as "empty response content" (F.8), which
                    # hid that the model had wanted a tool at all.
                    detail = str(candidate.get("finishMessage") or "").strip().replace("\n", " ")[:160]
                    return AIResult(
                        False,
                        error="malformed tool call from the model (Gemini finishReason %s)%s"
                              % (finish_reason, f": {detail}" if detail else ""),
                        tool_history=_history(),
                    )
                if finish_reason == "MAX_TOKENS":
                    # See GEMINI_DEFAULT_MAX_TOKENS's docstring: this is the
                    # thinking-budget-ate-everything failure mode, not a
                    # generic empty response, and it's worth saying so —
                    # "empty response content" gives no hint that the fix is
                    # a config change (raise max_tokens, or set
                    # thinkingConfig.thinkingBudget=0), and this failure
                    # otherwise reproduces identically on every key, which
                    # looks like a mass outage rather than one bad setting.
                    thoughts = ((data.get("usageMetadata") or {}).get("thoughtsTokenCount"))
                    hint = (
                        " (%d tokens spent on internal \"thinking\" before any "
                        "visible output)" % thoughts if thoughts else ""
                    )
                    return AIResult(
                        False,
                        error=(
                            "hit max_tokens with no visible output%s — raise "
                            "this provider's max_tokens (current cap: %d), or "
                            "set \"extra_params\": {\"thinkingConfig\": "
                            "{\"thinkingBudget\": 0}} in this provider's config "
                            "if you don't need reasoning traces"
                            % (hint, provider.get("max_tokens", GEMINI_DEFAULT_MAX_TOKENS))
                        ),
                        tool_history=_history(),
                    )
                return AIResult(False, error="empty response content", tool_history=_history())
            return AIResult(True, text=text, usage=get_usage_summary())

        return AIResult(False, error=_give_up_error(), tool_history=_history())
    finally:
        # Deliberately NOT deleting cache_name here. The previous version did,
        # and that single line is what made explicit caching a net loss: the
        # cache was destroyed at the end of the ask that created it, so the
        # write could never be amortized across the next jarvis process. It
        # now expires on its own TTL and is reused in the meantime (see
        # gemini_cache_store.py). The only deletion left is the one in the
        # loop above, for a cache that has genuinely gone stale.
        pass


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

def call_cohere(provider, messages, timeout, tools=None, tool_executor=None, round_budget=None,
                cfg_defaults=None):
    base_url = provider.get("base_url") or "https://api.cohere.com/v2/chat"
    api_key = provider.get("api_key") or ""
    model = provider.get("model") or ""
    if not api_key:
        return AIResult(False, error="no api_key configured")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # _merge_system: ai_client emits the system prompt as two messages
    # (static prefix + per-request tail) so call_anthropic can put a cache
    # breakpoint between them. Every other provider gets them folded back
    # into the single system message it has always received — byte-identical
    # to the pre-caching payload, since both sides join with "\n\n".
    working_messages = _merge_system(messages)
    ran_tools = False
    if round_budget is None:
        round_budget = RoundBudget()

    def _tools_payload():
        # Phase 9 (see new_plan.md): rebuilt every round so a Phase 5
        # search_tools match, appended to the same `tools` list object by
        # ai_client._make_tool_executor's discover_sink, is offered
        # starting next round.
        if not tools:
            return None
        return [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        # Trim tool results from earlier rounds before rebuilding this
        # round's payload — see _compact_prior_tool_results. Without this,
        # round N resends rounds 1..N-1's results at full size every time.
        _compact_prior_tool_results(working_messages)
        if tools and _forced_ending_due(round_num, round_budget, ran_tools, tool_executor):
            return _forced_ending(call_cohere, provider, _openai_messages_to_generic(working_messages),
                                  timeout, tools, tool_executor, round_budget, cfg_defaults, round_num)
        tools_payload = _tools_payload()
        payload = {
            "model": model,
            "messages": working_messages,
            "max_tokens": provider.get("max_tokens", 700),
        }
        if tools_payload and round_num < MAX_TOOL_ROUNDS and round_budget.remaining() > 0:
            payload["tools"] = tools_payload
        elif tools_payload:
            # F.1: withheld and the model is told so. Request-only.
            payload["messages"] = working_messages + [{"role": "user", "content": _TOOLS_WITHHELD_NOTICE}]

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

        _record_usage("cohere", data, round_num)

        message = data.get("message") or {}
        tool_calls = message.get("tool_calls")
        _capture_pending(_openai_style_calls(tool_calls))

        if tool_calls and tool_executor and round_num < MAX_TOOL_ROUNDS and round_budget.take():
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
                return AIResult(False, error=_give_up_error(), pending=_openai_style_calls(tool_calls),
                                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
            return AIResult(False, error="empty response content",
                            tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
        return AIResult(True, text=text, usage=get_usage_summary())

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

def call_ollama(provider, messages, timeout, tools=None, tool_executor=None, round_budget=None,
                cfg_defaults=None):
    base_url = provider.get("base_url") or "http://localhost:11434/api/chat"
    model = provider.get("model") or ""

    headers = {"Content-Type": "application/json"}
    # _merge_system: ai_client emits the system prompt as two messages
    # (static prefix + per-request tail) so call_anthropic can put a cache
    # breakpoint between them. Every other provider gets them folded back
    # into the single system message it has always received — byte-identical
    # to the pre-caching payload, since both sides join with "\n\n".
    working_messages = _merge_system(messages)
    ran_tools = False
    thought_rounds = 0
    system_parts_in, _ = _system_parts(messages)
    cache_plan = prompt_cache.plan(
        system_parts_in, None, provider, cfg_defaults or {}, model=model,
    )
    _log_cache_plan(cache_plan, "ollama")
    if round_budget is None:
        round_budget = RoundBudget()

    def _tools_payload():
        # Phase 9 (see new_plan.md): rebuilt every round so a Phase 5
        # search_tools match, appended to the same `tools` list object by
        # ai_client._make_tool_executor's discover_sink, is offered
        # starting next round.
        if not tools:
            return None
        return [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        # Trim tool results from earlier rounds before rebuilding this
        # round's payload — see _compact_prior_tool_results. Without this,
        # round N resends rounds 1..N-1's results at full size every time.
        _compact_prior_tool_results(working_messages)
        if tools and _forced_ending_due(round_num, round_budget, ran_tools, tool_executor):
            return _forced_ending(call_ollama, provider, _openai_messages_to_generic(working_messages),
                                  timeout, tools, tool_executor, round_budget, cfg_defaults, round_num)
        tools_payload = _tools_payload()
        payload = {"model": model, "messages": working_messages, "stream": False}
        if _apply_thinking(payload, provider, "ollama", round_num,
                           ran_tools, thought_rounds):
            thought_rounds += 1
        # Ollama reuses a KV prefix automatically when the prompt prefix
        # matches, but drops that cache when it unloads the model — which it
        # does after 5 minutes idle by default. jarvis is a fresh process per
        # call, often with minutes between calls, so that default means the
        # model unloads between asks and every ask pays a cold prefill.
        # Raising keep_alive is the entire optimization; there is no cache
        # field to set and nothing to bill.
        #
        # Costs VRAM residency on the user's own machine and nothing else.
        # Set "ollama_keep_alive": "0" in the provider block to restore
        # unload-immediately behavior on a machine that needs the memory.
        #
        # Do NOT try to measure hits with prompt_eval_count: it reports the
        # size of the prompt sent, not the tokens actually recomputed, so it
        # stays flat across a hit and a miss alike. prompt_eval_duration is
        # the field that moves (see token_usage.extract_usage).
        keep_alive = cache_plan.get("keep_alive")
        if keep_alive:
            payload["keep_alive"] = keep_alive
        if tools_payload and round_num < MAX_TOOL_ROUNDS and round_budget.remaining() > 0:
            payload["tools"] = tools_payload
        elif tools_payload:
            # F.1: withheld and the model is told so. Request-only.
            payload["messages"] = working_messages + [{"role": "user", "content": _TOOLS_WITHHELD_NOTICE}]

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

        _record_usage("ollama", data, round_num)
        _collect_thinking("ollama", data)

        message = data.get("message") or {}
        tool_calls = message.get("tool_calls")
        _capture_pending(_openai_style_calls(tool_calls))

        if tool_calls and tool_executor and round_num < MAX_TOOL_ROUNDS and round_budget.take():
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
                return AIResult(False, error=_give_up_error(), pending=_openai_style_calls(tool_calls),
                                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)
            return AIResult(
                False,
                error="empty response content (is the model pulled? try: ollama pull " + (model or "<model>") + ")",
                tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None,
            )
        return AIResult(True, text=text, usage=get_usage_summary())

    return AIResult(False, error=_give_up_error(),
                    tool_history=_openai_messages_to_generic(working_messages) if ran_tools else None)


ADAPTERS = {
    "openai_compatible": call_openai_compatible,
    "anthropic": call_anthropic,
    "gemini": call_gemini,
    "cohere": call_cohere,
    "ollama": call_ollama,
}