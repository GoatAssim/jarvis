"""Tests for three related fixes from the master plan's section 1c.

1. Optional tool parameters accept an explicit null on the wire to Groq.
   Groq validates tool-call arguments server-side, so a model that sends
   {"parent_id": null} for a {"type": "string"} property gets a 400 before
   jarvis ever sees the call ("expected string, but got null"). The catalog
   has ~290 optional scalar parameters, so this is done once at the wire
   boundary (ai_providers.nullable_optional_parameters), not per tool.
2. tools._drop_null_optionals makes that null reach the handler as "argument
   omitted", which is what handlers written `args.get("x", default)` expect.
3. When tools exist but are withheld for the round (round cap or the shared
   cross-key budget is spent), the request tells the model not to call one,
   instead of just silently omitting `tools` and letting it try anyway.

No network: requests.post is replaced by a queue of canned responses.
~/.jarvis is redirected to a temp dir before any jarvis import.

Run: python3 tests/test_nullable_optionals_and_withheld_tools.py
"""

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers, tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakePostQueue:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        # Deep copy: the adapter mutates one payload dict in place for its retry.
        self.calls.append({"url": url, "payload": copy.deepcopy(json)})
        if not self.responses:
            raise AssertionError("FakePostQueue exhausted")
        return self.responses.pop(0)


def _patch_post(responses):
    orig = ai_providers.requests.post
    fake = FakePostQueue(responses)
    ai_providers.requests.post = fake
    return fake, (lambda: setattr(ai_providers.requests, "post", orig))


GROQ = {"name": "groq", "type": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1/chat/completions", "api_key": "k", "model": "m"}
OPENAI = {"name": "openai", "type": "openai_compatible",
          "base_url": "https://api.openai.com/v1/chat/completions", "api_key": "k", "model": "m"}
OK_BODY = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}


def _spawn_schema():
    return [s for s in tools.TOOL_SCHEMAS if s["name"] == "spawn_subagent"][0]


# ---------------------------------------------------------------------------
# 1. Wire-level widening
# ---------------------------------------------------------------------------

def test_which_providers_get_the_widening():
    f = ai_providers.provider_wants_nullable_optionals
    check("groq by name", f(GROQ))
    check("a second groq entry by name prefix", f({"name": "groq2", "base_url": "https://x.example/v1"}))
    check("groq by host even under another name", f({"name": "fast", "base_url": "https://api.groq.com/openai/v1"}))
    check("openai is left alone by default", not f(OPENAI))
    check("explicit true turns it on elsewhere", f({**OPENAI, "nullable_optional_params": True}))
    check("explicit false turns it off for groq", not f({**GROQ, "nullable_optional_params": False}))
    check("no provider dict at all is fine", not f(None))


def test_widening_touches_only_optional_plain_typed_properties():
    params = {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "parent_id": {"type": "string", "description": "d"},
            "max_steps": {"type": "integer"},
            "flag": {"type": "boolean"},
            "ids": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "already": {"type": ["string", "null"]},
            "untyped": {"description": "no type"},
        },
        "required": ["role"],
    }
    before = copy.deepcopy(params)
    out = ai_providers.nullable_optional_parameters(params)
    p = out["properties"]
    check("the input schema is not mutated", params == before)
    check("a required property stays strict", p["role"] == {"type": "string"}, p["role"])
    check("optional string becomes nullable, keeping its description",
          p["parent_id"] == {"type": ["string", "null"], "description": "d"}, p["parent_id"])
    check("optional integer becomes nullable", p["max_steps"]["type"] == ["integer", "null"])
    check("optional boolean becomes nullable", p["flag"]["type"] == ["boolean", "null"])
    check("optional array becomes nullable and keeps items",
          p["ids"]["type"] == ["array", "null"] and p["ids"]["items"] == {"type": "string"}, p["ids"])
    check("an enum gains None so null doesn't fail the enum instead",
          p["mode"]["enum"] == ["a", "b", None] and p["mode"]["type"] == ["string", "null"], p["mode"])
    check("an already-nullable property is untouched", p["already"] == {"type": ["string", "null"]})
    check("a property with no type is untouched", p["untyped"] == {"description": "no type"})
    check("the original enum list was not modified", params["properties"]["mode"]["enum"] == ["a", "b"])


def test_widening_is_a_noop_when_there_is_nothing_to_widen():
    no_props = {"type": "object", "properties": {}, "required": []}
    all_required = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    check("empty properties returns the same object", ai_providers.nullable_optional_parameters(no_props) is no_props)
    check("all-required returns the same object",
          ai_providers.nullable_optional_parameters(all_required) is all_required)
    check("a non-dict passes through", ai_providers.nullable_optional_parameters(None) is None)


def test_groq_payload_declares_spawn_subagent_parent_id_nullable():
    # The exact tool and parameter from the logged 400.
    fake, restore = _patch_post([FakeResponse(200, OK_BODY)])
    try:
        ai_providers.call_openai_compatible(
            GROQ, [{"role": "user", "content": "hi"}], timeout=5,
            tools=[_spawn_schema()], tool_executor=None, round_budget=ai_providers.RoundBudget(),
        )
        sent = fake.calls[0]["payload"]["tools"][0]["function"]["parameters"]["properties"]
        check("parent_id can be null on the wire to Groq", sent["parent_id"]["type"] == ["string", "null"], sent["parent_id"])
        check("notes and max_steps too (same class of optional argument)",
              sent["notes"]["type"] == ["string", "null"] and sent["max_steps"]["type"] == ["integer", "null"])
        check("required role/goal stay strict strings",
              sent["role"]["type"] == "string" and sent["goal"]["type"] == "string")
    finally:
        restore()


def test_other_providers_payload_is_unchanged():
    fake, restore = _patch_post([FakeResponse(200, OK_BODY)])
    try:
        ai_providers.call_openai_compatible(
            OPENAI, [{"role": "user", "content": "hi"}], timeout=5,
            tools=[_spawn_schema()], tool_executor=None, round_budget=ai_providers.RoundBudget(),
        )
        sent = fake.calls[0]["payload"]["tools"][0]["function"]["parameters"]["properties"]
        check("openai still gets the source schema verbatim", sent["parent_id"]["type"] == "string", sent["parent_id"])
    finally:
        restore()


def test_source_catalog_schemas_are_not_modified_by_sending():
    before = copy.deepcopy(_spawn_schema())
    fake, restore = _patch_post([FakeResponse(200, OK_BODY)])
    try:
        ai_providers.call_openai_compatible(
            GROQ, [{"role": "user", "content": "hi"}], timeout=5,
            tools=[_spawn_schema()], tool_executor=None, round_budget=ai_providers.RoundBudget(),
        )
    finally:
        restore()
    check("the shared TOOL_SCHEMAS entry is byte-identical afterwards", _spawn_schema() == before)


# ---------------------------------------------------------------------------
# 2. null -> omitted at execution
# ---------------------------------------------------------------------------

def test_optional_nulls_are_dropped_before_the_handler_runs():
    out = tools._drop_null_optionals("spawn_subagent", {"role": "r", "goal": "g", "parent_id": None, "notes": None})
    check("optional nulls vanish", out == {"role": "r", "goal": "g"}, out)


def test_null_drop_is_narrow():
    same = {"role": None, "goal": "g"}
    check("a REQUIRED null is left for the normal missing-argument handling",
          tools._drop_null_optionals("spawn_subagent", dict(same)) == same)
    unknown_key = {"role": "r", "goal": "g", "invented": None}
    check("a null under a key the schema doesn't declare is left alone",
          tools._drop_null_optionals("spawn_subagent", dict(unknown_key)) == unknown_key)
    nested = {"role": "r", "goal": "g", "task_ids": [None]}
    check("nulls nested inside a value are left alone",
          tools._drop_null_optionals("spawn_subagent", dict(nested)) == nested)
    check("a tool with no known schema is left alone",
          tools._drop_null_optionals("some_user_defined_tool", {"x": None}) == {"x": None})
    check("arguments with no None are returned as-is (same object)",
          (lambda a: tools._drop_null_optionals("spawn_subagent", a) is a)({"role": "r", "goal": "g"}))
    check("non-dict arguments pass through", tools._drop_null_optionals("spawn_subagent", None) is None)


def test_execute_tool_hands_the_handler_the_cleaned_arguments():
    seen = {}
    orig = tools.TOOLS["spawn_subagent"]
    tools.TOOLS["spawn_subagent"] = lambda args: seen.update(args) or {"ok": True}
    try:
        tools.execute_tool("spawn_subagent", {"role": "r", "goal": "g", "parent_id": None, "max_steps": None})
    finally:
        tools.TOOLS["spawn_subagent"] = orig
    check("the handler never sees the null keys", seen == {"role": "r", "goal": "g"}, seen)


# ---------------------------------------------------------------------------
# 3. Withheld-tools notice
# ---------------------------------------------------------------------------

def _exhausted_budget():
    b = ai_providers.RoundBudget()
    b.remaining = lambda: 0
    b.take = lambda: False
    return b


TOOLS = [{"name": "web_search", "description": "search", "parameters": {"type": "object", "properties": {}}}]


def test_notice_is_added_when_tools_are_withheld():
    fake, restore = _patch_post([FakeResponse(200, OK_BODY)])
    try:
        ai_providers.call_openai_compatible(
            GROQ, [{"role": "user", "content": "hi"}], timeout=5,
            tools=TOOLS, tool_executor=None, round_budget=_exhausted_budget(),
        )
        p = fake.calls[0]["payload"]
        check("tools are omitted from the request", "tools" not in p)
        last = p["messages"][-1]
        check("the last message tells the model not to call tools",
              last["role"] == "user" and "Do not call any tool" in last["content"], last)
        check("the real user message is still first", p["messages"][0]["content"] == "hi")
    finally:
        restore()


def test_no_notice_when_tools_are_offered():
    fake, restore = _patch_post([FakeResponse(200, OK_BODY)])
    try:
        ai_providers.call_openai_compatible(
            GROQ, [{"role": "user", "content": "hi"}], timeout=5,
            tools=TOOLS, tool_executor=None, round_budget=ai_providers.RoundBudget(),
        )
        p = fake.calls[0]["payload"]
        check("tools are attached", "tools" in p)
        check("no notice is added", [m["content"] for m in p["messages"]] == ["hi"], p["messages"])
    finally:
        restore()


def test_no_notice_when_the_caller_never_offered_tools():
    fake, restore = _patch_post([FakeResponse(200, OK_BODY)])
    try:
        ai_providers.call_openai_compatible(
            GROQ, [{"role": "user", "content": "hi"}], timeout=5,
            tools=None, tool_executor=None, round_budget=ai_providers.RoundBudget(),
        )
        check("a plain no-tools call is untouched",
              [m["content"] for m in fake.calls[0]["payload"]["messages"]] == ["hi"])
    finally:
        restore()


def test_retry_with_tools_drops_the_contradictory_notice():
    err = {"error": {"message": "Tool choice is none, but model called a tool"}}
    fake, restore = _patch_post([FakeResponse(400, err), FakeResponse(200, OK_BODY)])
    try:
        result = ai_providers.call_openai_compatible(
            GROQ, [{"role": "user", "content": "hi"}], timeout=5,
            tools=TOOLS, tool_executor=None, round_budget=_exhausted_budget(),
        )
        first, retry = fake.calls[0]["payload"], fake.calls[1]["payload"]
        check("first request carried the notice", first["messages"][-1]["role"] == "user"
              and "Do not call any tool" in first["messages"][-1]["content"])
        check("the retry re-attached tools", "tools" in retry)
        check("the retry no longer says 'do not call tools'",
              [m["content"] for m in retry["messages"]] == ["hi"], retry["messages"])
        check("and the retry's answer comes through", result.ok and result.text == "done", (result.ok, result.error))
    finally:
        restore()


for fn in [
    test_which_providers_get_the_widening,
    test_widening_touches_only_optional_plain_typed_properties,
    test_widening_is_a_noop_when_there_is_nothing_to_widen,
    test_groq_payload_declares_spawn_subagent_parent_id_nullable,
    test_other_providers_payload_is_unchanged,
    test_source_catalog_schemas_are_not_modified_by_sending,
    test_optional_nulls_are_dropped_before_the_handler_runs,
    test_null_drop_is_narrow,
    test_execute_tool_hands_the_handler_the_cleaned_arguments,
    test_notice_is_added_when_tools_are_withheld,
    test_no_notice_when_tools_are_offered,
    test_no_notice_when_the_caller_never_offered_tools,
    test_retry_with_tools_drops_the_contradictory_notice,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
