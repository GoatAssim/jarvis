"""Tests for subagents.py — above all, the key-isolation guarantee.

Run with `python3 tests/test_subagents.py`. No API key or network needed.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import subagents, tasks, ai_client  # noqa: E402


MAIN_KEY = "sk-MAIN-do-not-spend"
CODER_KEYS = ["sk-coder-1", "sk-coder-2"]
RESEARCH_KEYS = ["sk-research-1"]

PROVIDERS = [
    {"name": "anthropic", "type": "anthropic", "model": "m", "api_keys": [MAIN_KEY]},
    {"name": "gemini", "type": "gemini", "model": "g", "api_key": MAIN_KEY},
    {"name": "groq", "type": "openai_compatible", "model": "q", "api_keys": ["sk-groq-main"]},
]


def _isolate():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-subagent-test-"))
    tasks.TASKS_DIR = tmp / "tasks"
    subagents.CONFIG_FILE = tmp / "subagents.json"
    os.environ.pop(subagents.KEY_ENV, None)
    os.environ.pop(subagents.ROLE_ENV, None)
    return tmp


def _write_pools():
    subagents.save_config({
        "max_concurrent": 2,
        "key_pools": {
            "coder": {"provider": "anthropic", "api_keys": CODER_KEYS},
            "research": {"provider": "gemini", "api_keys": RESEARCH_KEYS},
        },
        "agents": {},
    })


# ---------------------------------------------------------------------------


def test_builtin_agents_exist():
    _isolate()
    known = subagents.agents()
    for role in ("coder", "research", "monitor"):
        assert role in known, role
        assert known[role]["prompt"]
        assert known[role]["builtin"] is True
    print("ok  builtin agents")


def test_custom_agent_merges_over_builtin():
    _isolate()
    subagents.save_config({
        "key_pools": {},
        "agents": {
            "research": {"think": "high"},                 # override one field
            "auditor": {"prompt": "You audit things.", "max_steps": 5},  # brand new
        },
    })
    known = subagents.agents()
    assert known["research"]["think"] == "high"
    assert known["research"]["prompt"], "unspecified fields keep the builtin value"
    assert known["auditor"]["max_steps"] == 5
    assert known["auditor"]["builtin"] is False
    print("ok  custom agents")


def test_no_pool_means_no_spawn():
    """The single most important behavior: absent a dedicated key pool, a
    subagent must refuse to exist rather than inherit the main key."""
    _isolate()
    try:
        subagents.spawn("coder", "do a thing")
    except subagents.SubagentError as exc:
        assert "never use the main" in str(exc)
    else:
        raise AssertionError("spawned a subagent with no key pool")
    print("ok  refuses to spawn without a key pool")


def test_key_env_pins_to_own_pool():
    _isolate()
    _write_pools()
    env = subagents.key_env("coder")
    pool = json.loads(env[subagents.KEY_ENV])
    assert pool["provider"] == "anthropic"
    assert pool["api_keys"] == CODER_KEYS
    assert MAIN_KEY not in pool["api_keys"]
    assert env[subagents.ROLE_ENV] == "coder"

    # A role with no pool and no "default" gets nothing at all.
    assert subagents.key_env("monitor") == {}
    print("ok  key_env")


def test_default_pool_is_shared_fallback():
    _isolate()
    subagents.save_config({
        "key_pools": {"default": {"provider": "groq", "api_keys": ["sk-shared"]}},
        "agents": {},
    })
    pool = subagents.key_pool("monitor")
    assert pool["api_keys"] == ["sk-shared"]
    assert pool["provider"] == "groq"
    print("ok  default pool fallback")


def test_eligible_providers_replaces_not_extends():
    """The isolation guarantee, checked at the exact chokepoint."""
    _isolate()
    _write_pools()

    # Normal ask: untouched.
    normal = ai_client._eligible_providers(PROVIDERS, {})
    assert len(normal) == 3
    assert any(MAIN_KEY in (p.get("api_keys") or []) or p.get("api_key") == MAIN_KEY
               for p in normal)

    # As a coder subagent: exactly one provider, exactly that pool's keys.
    os.environ.update(subagents.key_env("coder"))
    try:
        pinned = ai_client._eligible_providers(PROVIDERS, {})
        assert len(pinned) == 1, pinned
        assert pinned[0]["name"] == "anthropic"
        assert pinned[0]["api_keys"] == CODER_KEYS
        # Neither the main key nor any OTHER subagent's key is reachable,
        # on the first try or on failover.
        from jarvis import ai_config
        reachable = ai_config.provider_keys(pinned[0])
        assert MAIN_KEY not in reachable
        assert RESEARCH_KEYS[0] not in reachable
        assert "sk-groq-main" not in reachable
        assert reachable == CODER_KEYS, "failover order must be the pool's order"
    finally:
        os.environ.pop(subagents.KEY_ENV, None)
        os.environ.pop(subagents.ROLE_ENV, None)

    print("ok  eligible_providers replaces the whole list")


def test_legacy_singular_api_key_is_dropped():
    """gemini's config uses the legacy singular 'api_key' holding the main
    key. Replacing only 'api_keys' would leave it reachable."""
    _isolate()
    _write_pools()
    os.environ.update(subagents.key_env("research"))
    try:
        pinned = ai_client._eligible_providers(PROVIDERS, {})
        assert len(pinned) == 1
        assert "api_key" not in pinned[0], "legacy singular field must be stripped"
        from jarvis import ai_config
        assert ai_config.provider_keys(pinned[0]) == RESEARCH_KEYS
    finally:
        os.environ.pop(subagents.KEY_ENV, None)
        os.environ.pop(subagents.ROLE_ENV, None)
    print("ok  legacy api_key field stripped")


def test_malformed_pool_yields_nothing_not_main_key():
    _isolate()
    for bad in ("{not json", json.dumps({"provider": "", "api_keys": []}),
                json.dumps({"provider": "anthropic", "api_keys": []}),
                json.dumps({"provider": "nonexistent", "api_keys": ["k"]})):
        os.environ[subagents.KEY_ENV] = bad
        try:
            out = ai_client._eligible_providers(PROVIDERS, {})
        finally:
            os.environ.pop(subagents.KEY_ENV, None)
        # Unparseable JSON is indistinguishable from "not a subagent", so it
        # falls through; every other malformed shape must yield nothing.
        if bad == "{not json":
            continue
        assert out == [], (bad, out)
    print("ok  malformed pools never fall back to the main key")


def test_spawn_creates_a_normal_task():
    _isolate()
    _write_pools()
    parent = tasks.create("big job")
    kid = subagents.spawn("coder", "fix the build", parent_id=parent["id"])
    assert kid["agent"] == "coder"
    assert kid["parent_id"] == parent["id"]
    assert kid["think"] == "medium"
    assert "coding subagent" in kid["notes"]
    assert kid["allowed_tools"] and "dev_agent" in kid["allowed_tools"]
    # It is an ordinary task: the same supervisor picks it up.
    assert tasks.is_runnable(kid)
    print("ok  spawn")


def test_pool_size_is_enforced():
    _isolate()
    _write_pools()  # max_concurrent = 2
    subagents.spawn("coder", "a")
    subagents.spawn("research", "b")
    try:
        subagents.spawn("coder", "c")
    except subagents.SubagentError as exc:
        assert "pool is full" in str(exc)
    else:
        raise AssertionError("pool size not enforced")
    print("ok  pool size")


def test_aggregate_escalates_on_disagreement():
    _isolate()
    _write_pools()
    parent = tasks.create("decide something")

    a = subagents.spawn("coder", "check A", parent_id=parent["id"])
    b = subagents.spawn("research", "check B", parent_id=parent["id"])
    tasks.finish(a, tasks.STATUS_DONE, result="Yes, this works and the tests passed.")
    tasks.finish(b, tasks.STATUS_DONE, result="No, this is broken and unsafe.")

    agg = subagents.aggregate(parent["id"])
    assert agg["all_finished"] is True
    assert agg["escalate"] is True
    assert "subagents disagree" in agg["escalation_reasons"]

    digest = subagents.summary_for_parent(parent["id"])
    assert "NEEDS A HUMAN" in digest
    print("ok  disagreement escalates")


def test_aggregate_agrees_quietly():
    _isolate()
    _write_pools()
    parent = tasks.create("p")
    a = subagents.spawn("coder", "x", parent_id=parent["id"])
    b = subagents.spawn("research", "y", parent_id=parent["id"])
    tasks.finish(a, tasks.STATUS_DONE, result="Yes, confirmed working.")
    tasks.finish(b, tasks.STATUS_DONE, result="Correct, this passes fine.")
    agg = subagents.aggregate(parent["id"])
    assert agg["escalate"] is False, agg["escalation_reasons"]
    assert len(agg["done"]) == 2

    # One child alone can never "disagree".
    assert subagents._disagreement([{"result": "no, broken"}]) is False
    print("ok  agreement is quiet")


def test_blocked_child_escalates():
    _isolate()
    _write_pools()
    parent = tasks.create("p")
    kid = subagents.spawn("coder", "x", parent_id=parent["id"])
    tasks.finish(kid, tasks.STATUS_BLOCKED, error="which database?")
    agg = subagents.aggregate(parent["id"])
    assert agg["escalate"] is True
    assert "need a decision" in agg["escalation_reasons"][0]
    print("ok  blocked child escalates")


def test_describe_pools_never_prints_a_key():
    _isolate()
    _write_pools()
    described = json.dumps(subagents.describe_pools())
    for key in CODER_KEYS + RESEARCH_KEYS:
        assert key not in described, "a full key leaked into the summary"
    assert "coder" in described and "anthropic" in described
    print("ok  pool summary redacts keys")


if __name__ == "__main__":
    test_builtin_agents_exist()
    test_custom_agent_merges_over_builtin()
    test_no_pool_means_no_spawn()
    test_key_env_pins_to_own_pool()
    test_default_pool_is_shared_fallback()
    test_eligible_providers_replaces_not_extends()
    test_legacy_singular_api_key_is_dropped()
    test_malformed_pool_yields_nothing_not_main_key()
    test_spawn_creates_a_normal_task()
    test_pool_size_is_enforced()
    test_aggregate_escalates_on_disagreement()
    test_aggregate_agrees_quietly()
    test_blocked_child_escalates()
    test_describe_pools_never_prints_a_key()
    print("\nall subagent tests passed")
