"""L.24 T6 (per-ask token total, thinking included) and T1 (router misses).

Standalone, plain-assert, like the rest of tests/. Run directly:
    python3 tests/test_l24_token_cost.py

The numbers asserted here are MEASURED from the two real conversation logs in
tests/fixtures (the F.17 pairs), not invented; tests/measure_l24.py prints the
same measurements. The two logs the plan's L.24 table was built from
(5267d91a8b996d48, 1a99e1e3f0d3af0e) were not in the repository, so the
router half is pinned on their prompts instead of their traffic.
"""

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import (  # noqa: E402
    ai_client, ai_config, ai_providers, conversations, logs, token_usage,
    tool_loader, tool_registry, tool_router, turn_trace,
)
from jarvis import tools as system_tools  # noqa: E402

tool_loader.discover_actions()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


FIXTURES = ROOT / "tests" / "fixtures"


def _rows(name):
    out = []
    for line in (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# T6 - extraction
# ---------------------------------------------------------------------------

def test_gemini_thinking_is_extracted_and_additive():
    body = {"usageMetadata": {"promptTokenCount": 1192, "candidatesTokenCount": 27,
                              "totalTokenCount": 1861, "thoughtsTokenCount": 642}}
    u = token_usage.extract_usage("gemini", body)
    check("gemini: thinking_tokens extracted", u.get("thinking_tokens") == 642, u)
    check("gemini: output_tokens still excludes thinking", u["output_tokens"] == 27, u)
    check("gemini: entry_total adds thinking (= totalTokenCount)",
          token_usage.entry_total(u) == 1861, token_usage.entry_total(u))
    plain = token_usage.extract_usage("gemini", {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}})
    check("gemini: no thoughtsTokenCount -> no thinking key", "thinking_tokens" not in plain, plain)


def test_every_real_gemini_response_matches_provider_total():
    """The strongest check available: for every Gemini response in the two
    real logs, input + output + thinking must equal the provider's own
    totalTokenCount. If thinking were already inside output this would fail."""
    checked = bad = 0
    for name in ("81b52bb561796954", "cb140e091ddc66e8"):
        for r in _rows(name):
            body = (r["data"] or {}).get("body") if r["direction"] == "response" else None
            if not isinstance(body, dict):
                continue
            meta = body.get("usageMetadata")
            if not isinstance(meta, dict) or meta.get("totalTokenCount") is None:
                continue
            u = token_usage.extract_usage("gemini", body)
            checked += 1
            if token_usage.entry_total(u) != meta["totalTokenCount"]:
                bad += 1
    check("real logs: Gemini responses with a totalTokenCount were found", checked >= 30, checked)
    check("real logs: input+output+thinking == totalTokenCount on every one", bad == 0, f"{bad} of {checked}")


def test_openai_reasoning_tokens_are_not_double_counted():
    body = {"usage": {"prompt_tokens": 100, "completion_tokens": 300,
                      "completion_tokens_details": {"reasoning_tokens": 250}}}
    u = token_usage.extract_usage("openai_compatible", body)
    check("openai: reasoning tokens reported as thinking", u.get("thinking_tokens") == 250, u)
    check("openai: ...but flagged as already inside output", u.get("thinking_in_output") is True, u)
    check("openai: total is input+output only", token_usage.entry_total(u) == 400, token_usage.entry_total(u))


def test_other_providers_unchanged():
    a = token_usage.extract_usage("anthropic", {"usage": {"input_tokens": 10, "output_tokens": 5}})
    check("anthropic: unchanged shape", a == {"input_tokens": 10, "output_tokens": 5, "source": "reported"}, a)
    o = token_usage.extract_usage("ollama", {"prompt_eval_count": 7, "eval_count": 3})
    check("ollama: no thinking key invented", "thinking_tokens" not in o, o)


# ---------------------------------------------------------------------------
# T6 - get_usage_summary + AskUsage
# ---------------------------------------------------------------------------

def test_attempt_summary_counts_thinking():
    ai_providers.set_log_context(None, "t")
    try:
        ai_providers._record_usage("gemini", {"usageMetadata": {
            "promptTokenCount": 100, "candidatesTokenCount": 10, "thoughtsTokenCount": 50}}, 0)
        ai_providers._record_usage("gemini", {"usageMetadata": {
            "promptTokenCount": 200, "candidatesTokenCount": 20}}, 1)
        s = ai_providers.get_usage_summary()
    finally:
        ai_providers.clear_log_context()
    check("summary: input/output unchanged", (s["input_tokens"], s["output_tokens"]) == (300, 30), s)
    check("summary: thinking_tokens reported", s.get("thinking_tokens") == 50, s)
    check("summary: total_tokens includes thinking", s["total_tokens"] == 380, s["total_tokens"])


def _ask_usage_from_fixture(name):
    """Rebuild each attempt the way ask() would, from the logged raw responses
    (they carry thoughtsTokenCount, the usage rows do not)."""
    by_label, order = {}, []
    for r in _rows(name):
        body = (r["data"] or {}).get("body") if r["direction"] == "response" else None
        if not isinstance(body, dict):
            continue
        provider_type = "gemini" if "usageMetadata" in body else "openai_compatible"
        u = token_usage.extract_usage(provider_type, body)
        if u is None:
            continue
        label = r["provider"]
        if label not in by_label:
            by_label[label] = []
            order.append(label)
        by_label[label].append({"round": r["round"], **u})
    ask = token_usage.AskUsage()
    for i, label in enumerate(order):
        ask.add(label, {"rounds": by_label[label]}, ok=(i == len(order) - 1))
    return ask.to_dict(), by_label, order


def test_per_ask_total_on_real_failover_logs():
    for name, exp_in_out, exp_think, exp_inout, exp_attempts in (
        ("81b52bb561796954", 49276, 7256, 508, 10),
        ("cb140e091ddc66e8", 35444, 3702, 1129, 13),
    ):
        total, by_label, order = _ask_usage_from_fixture(name)
        check(f"{name}: attempts counted", total["attempt_count"] == exp_attempts, total["attempt_count"])
        check(f"{name}: input+output across ALL attempts == logged usage rows",
              total["input_tokens"] + total["output_tokens"] == exp_in_out,
              total["input_tokens"] + total["output_tokens"])
        check(f"{name}: Gemini thinking tokens counted (independent regex oracle)",
              total["thinking_tokens"] == exp_think, total["thinking_tokens"])
        check(f"{name}: Groq reasoning tokens are reported but NOT added again",
              total["thinking_in_output_tokens"] == exp_inout, total["thinking_in_output_tokens"])
        check(f"{name}: total_tokens = in + out + thinking",
              total["total_tokens"] == exp_in_out + exp_think, total["total_tokens"])
        last = total["attempts"][-1]
        check(f"{name}: the old figure (answering attempt only) was under 10% of the real spend",
              last["total_tokens"] * 10 < total["total_tokens"], (last["total_tokens"], total["total_tokens"]))


def test_ask_usage_skips_attempts_that_made_no_request():
    ask = token_usage.AskUsage()
    ask.add("a", None)
    ask.add("b", {"rounds": []})
    check("empty attempts are not recorded", not ask and ask.to_dict()["attempt_count"] == 0)
    check("format_ask_total of nothing is empty", token_usage.format_ask_total(ask.to_dict()) == "")
    ask.add("c", {"rounds": [{"input_tokens": 10, "output_tokens": 2, "thinking_tokens": 5}]}, ok=True)
    line = token_usage.format_ask_total(ask.to_dict())
    check("format_ask_total mentions total and thinking", "total=17" in line and "thinking=5" in line, line)


# ---------------------------------------------------------------------------
# T6 - end to end through ask(): failed attempt + answering attempt
# ---------------------------------------------------------------------------

@contextmanager
def _isolated_ask_env():
    orig = {
        "conv_dir": conversations.JARVIS_DIR, "conv_conv_dir": conversations.CONV_DIR,
        "conv_index": conversations.INDEX_FILE, "conv_current": conversations.CURRENT_FILE,
        "logs_dir": logs.JARVIS_DIR, "logs_log_dir": logs.LOG_DIR,
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR = tmp
        logs.LOG_DIR = tmp / "logs"
        try:
            yield conversations.get_current_id()
        finally:
            conversations.JARVIS_DIR = orig["conv_dir"]
            conversations.CONV_DIR = orig["conv_conv_dir"]
            conversations.INDEX_FILE = orig["conv_index"]
            conversations.CURRENT_FILE = orig["conv_current"]
            logs.JARVIS_DIR = orig["logs_dir"]
            logs.LOG_DIR = orig["logs_log_dir"]


@contextmanager
def _fake_two_provider_ask(second_succeeds=True):
    orig_load = ai_config.load_ai_config
    orig_adapters = dict(ai_providers.ADAPTERS)
    cfg = {
        "persona": {},
        "providers": [
            {"name": "fakeprov1", "type": "fake", "enabled": True, "api_keys": ["k1"], "model": "m"},
            {"name": "fakeprov2", "type": "fake", "enabled": True, "api_keys": ["k2"], "model": "m"},
        ],
        "defaults": {"tools_enabled": False, "prompt_mode": "full"},
    }
    calls = []

    def fake_adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        n = len(calls)
        calls.append(resolved.get("api_key"))
        # Every request reports usage the way the real adapters do.
        ai_providers._record_usage("gemini", {"usageMetadata": {
            "promptTokenCount": 1000, "candidatesTokenCount": 10, "thoughtsTokenCount": 300}}, 0)
        if n == 0 or not second_succeeds:
            return ai_providers.AIResult(False, error="HTTP 503 model overloaded")
        return ai_providers.AIResult(True, text="all done", usage=ai_providers.get_usage_summary())

    ai_config.load_ai_config = lambda: cfg
    ai_providers.ADAPTERS["fake"] = fake_adapter
    try:
        yield calls
    finally:
        ai_config.load_ai_config = orig_load
        ai_providers.ADAPTERS.clear()
        ai_providers.ADAPTERS.update(orig_adapters)


def test_ask_total_spans_a_failed_attempt():
    with _isolated_ask_env() as conv_id, _fake_two_provider_ask() as calls:
        result = ai_client.ask("hello there", commands=[], conversation_id=conv_id)
        check("ask succeeded on the second provider", result.ok and result.text == "all done", result.text)
        check("two provider calls were made", len(calls) == 2, calls)
        u = result.usage or {}
        at = u.get("ask_total") or {}
        check("usage carries ask_total", bool(at), u)
        check("ask_total counts BOTH attempts", at.get("attempt_count") == 2, at)
        check("ask_total = 2 x (1000 in + 10 out + 300 thinking)", at.get("total_tokens") == 2620, at)
        check("ask_total.thinking_tokens counted", at.get("thinking_tokens") == 600, at)
        check("top-level numbers are still the answering attempt only",
              u.get("input_tokens") == 1000 and u.get("total_tokens") == 1310, u)
        check("the failed attempt is marked failed, the winner answered",
              [a["ok"] for a in at.get("attempts", [])] == [False, True], at)
        saved = conversations.get_conversation(conv_id)
        extras = []
        for ex in (saved.get("exchanges") or []):
            extras.extend(ex.get("extras") or [])
        traces = [e for e in extras if e.get("type") == "trace"]
        tok = (traces[-1]["data"].get("tokens") if traces else None) or {}
        check("the saved turn trace records the ask total", tok.get("total_tokens") == 2620, traces)
        lines = turn_trace.TurnTrace().lines()
        check("an empty trace adds no cost line", not any("Spent" in ln for ln in lines), lines)


def test_ask_total_when_every_provider_fails():
    with _isolated_ask_env() as conv_id, _fake_two_provider_ask(second_succeeds=False):
        result = ai_client.ask("hello there", commands=[], conversation_id=conv_id)
        check("ask failed", result.ok is False)
        at = (result.usage or {}).get("ask_total") or {}
        check("a failed ask still reports what it cost", at.get("total_tokens") == 2620, result.usage)


def test_trace_line_reports_cost():
    t = turn_trace.TurnTrace()
    t.tokens = {"attempt_count": 2, "total_tokens": 2620, "input_tokens": 2000,
                "output_tokens": 20, "thinking_tokens": 600, "rounds": 2}
    line = [ln for ln in t.lines() if ln.startswith("Spent")]
    check("trace line shows total, thinking and attempts",
          bool(line) and "2620" in line[0] and "600 thinking" in line[0] and "2 attempts" in line[0], line)
    check("trace dict exposes tokens", t.to_dict()["tokens"]["total_tokens"] == 2620)


# ---------------------------------------------------------------------------
# T1 - router
# ---------------------------------------------------------------------------

def _route(text):
    return tool_router.route(text)


def test_discord_question_no_longer_offers_desktop():
    r = _route("hey jarvis has anyone texted you on discord")
    check("5267 prompt routes to channels only", r.groups == ["channels"], r.groups)
    check("5267 prompt offers recent_dms", "recent_dms" in r.tools, r.tools)
    # Was 4. L.20 added send_dm to the channels group (notify_owner, recent_dms,
    # remember_sender, who_am_i_talking_to, send_dm). The point of this check is
    # "the channels group, not the 22-tool desktop group"; keep it exact so
    # unplanned growth of the group is still noticed.
    check("5267 prompt offers 5 tools, not 22", len(r.tools) == 5, (len(r.tools), r.tools))
    check("no desktop tool is offered", not any(t in r.tools for t in ("click", "read_screen", "take_screenshot", "focus_window")), r.tools)

    full = system_tools.compact_schemas_for_prompt(system_tools.schemas_for_tools(r.tools))
    old = system_tools.compact_schemas_for_prompt(system_tools.schemas_for_tools(
        tool_registry.tools_in_group("desktop") + tool_registry.tools_in_group("channels")))
    check("declarations per call shrink by more than 3x",
          token_usage.estimate_tokens_for(old) > 3 * token_usage.estimate_tokens_for(full),
          (token_usage.estimate_tokens_for(old), token_usage.estimate_tokens_for(full)))


def test_texted_phrasings_route_without_a_platform_word():
    for text in ("has anyone texted you", "who texted me", "did anyone text me today",
                 "anyone dm'd you lately", "did anyone dm you", "any new dms"):
        r = _route(text)
        check(f"'{text}' -> recent_dms", "recent_dms" in r.tools, (r.groups, r.tools))


def test_sending_a_text_does_not_route_to_recent_dms():
    for text in ("can you text me later", "text mom that i'm late", "remind me to text bob"):
        r = _route(text)
        # The channels GROUP may still be offered (notify_owner owns "text me"),
        # which brings its sibling tools along; what must not happen is
        # recent_dms being the tool that matched.
        check(f"'{text}' is not matched by a recent_dms keyword",
              not any(m[1] == "recent_dms" for m in r.matches), r.matches)


def test_discord_actions_still_route_to_desktop():
    for text in ("open discord", "Go on Discord, tag @no and say hi.", "click discord",
                 "send a message on discord to bob", "message John on discord and say hi",
                 "switch to discord"):
        r = _route(text)
        check(f"'{text}' still offers the desktop group", "desktop" in r.groups, (r.groups, r.matches))


def test_read_style_discord_questions_do_not_offer_desktop():
    for text in ("did anyone dm you on discord", "who dmed me on discord", "are there unread dms on discord"):
        r = _route(text)
        check(f"'{text}' does not offer desktop", "desktop" not in r.groups, (r.groups, r.matches))


def test_needs_any_unit():
    v = {"weight": 8, "needs_any": ["open", "go"], "not_with": ["anyone"]}
    check("keyword_needs reads the list", tool_registry.keyword_needs(v) == ["open", "go"])
    check("keyword_needs of a plain int is []", tool_registry.keyword_needs(8) == [])
    check("keyword_needs of a dict without it is []", tool_registry.keyword_needs({"weight": 8}) == [])
    check("keyword_weight / keyword_exclusions unaffected",
          tool_registry.keyword_weight(v) == 8 and tool_registry.keyword_exclusions(v) == ["anyone"])


def test_prompt_with_no_discord_is_unchanged():
    r = _route("Check usage and shutdown PC")
    check("scheduler prompt still routes to system_control", r.groups == ["system_control"], r.groups)
    r = _route("what's my battery status")
    check("battery still routes", "get_battery" in r.tools, r.tools)


for fn in [
    test_gemini_thinking_is_extracted_and_additive,
    test_every_real_gemini_response_matches_provider_total,
    test_openai_reasoning_tokens_are_not_double_counted,
    test_other_providers_unchanged,
    test_attempt_summary_counts_thinking,
    test_per_ask_total_on_real_failover_logs,
    test_ask_usage_skips_attempts_that_made_no_request,
    test_ask_total_spans_a_failed_attempt,
    test_ask_total_when_every_provider_fails,
    test_trace_line_reports_cost,
    test_discord_question_no_longer_offers_desktop,
    test_texted_phrasings_route_without_a_platform_word,
    test_sending_a_text_does_not_route_to_recent_dms,
    test_discord_actions_still_route_to_desktop,
    test_read_style_discord_questions_do_not_offer_desktop,
    test_needs_any_unit,
    test_prompt_with_no_discord_is_unchanged,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
