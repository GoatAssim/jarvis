"""Tests for this round's additions.

Same no-dependency, plain-assert pattern as tests/test_enhancements.py — run
directly:

    python3 tests/test_new_features.py

Every test that touches ~/.jarvis isolates itself to a temp directory first
(see _isolated), because a test suite that writes to the developer's real
memory/config is a test suite nobody runs twice.
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import reasoning, turn_trace, policy, timespec, digest  # noqa: E402
from jarvis import memory_semantic, ui_bridge, custom_tools_store   # noqa: E402
from jarvis import conv_export                                      # noqa: E402

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
    except AssertionError as exc:
        FAILED.append((name, str(exc) or "assertion failed"))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, "%s: %s" % (type(exc).__name__, exc)))


@contextmanager
def _isolated():
    """Point every module's ~/.jarvis path at a throwaway directory."""
    tmp = Path(tempfile.mkdtemp())
    jarvis_dir = tmp / ".jarvis"
    jarvis_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    targets = [
        (policy, ["JARVIS_DIR", "POLICY_FILE"]),
        (digest, ["JARVIS_DIR", "CONFIG_FILE", "QUEUE_FILE"]),
        (memory_semantic, ["JARVIS_DIR", "INDEX_FILE"]),
        (custom_tools_store, ["JARVIS_DIR", "TOOLS_DIR", "META_FILE"]),
        (timespec, ["HOLIDAYS_FILE"]),
    ]
    for module, names in targets:
        for name in names:
            if hasattr(module, name):
                saved[(module, name)] = getattr(module, name)
    policy.JARVIS_DIR, policy.POLICY_FILE = jarvis_dir, jarvis_dir / "policy.json"
    digest.JARVIS_DIR = jarvis_dir
    digest.CONFIG_FILE = jarvis_dir / "digest_config.json"
    digest.QUEUE_FILE = jarvis_dir / "digest_queue.json"
    memory_semantic.JARVIS_DIR = jarvis_dir
    memory_semantic.INDEX_FILE = jarvis_dir / "memory_index.json"
    custom_tools_store.JARVIS_DIR = jarvis_dir
    custom_tools_store.TOOLS_DIR = jarvis_dir / "tools"
    custom_tools_store.META_FILE = custom_tools_store.TOOLS_DIR / "_meta.json"
    timespec.HOLIDAYS_FILE = jarvis_dir / "holidays.json"
    try:
        yield jarvis_dir
    finally:
        for (module, name), value in saved.items():
            setattr(module, name, value)


# ---------------------------------------------------------------------------
# reasoning — token discipline is the whole point, so it gets the most tests
# ---------------------------------------------------------------------------


def test_level_normalization():
    assert reasoning.normalize_level("HIGH") == "high"
    assert reasoning.normalize_level(True) == "medium"
    assert reasoning.normalize_level(False) == "off"
    assert reasoning.normalize_level("deep") == "high"
    assert reasoning.normalize_level("nonsense") == "off"


def test_auto_escalates_only_when_worth_it():
    assert reasoning.auto_level("what's my battery") == "off"
    assert reasoning.auto_level("hi") == "off"
    assert reasoning.auto_level(
        "why is this build failing, walk me through the root cause step by step"
    ) in ("low", "medium")
    # Auto must never reach "high" on its own — that stays a typed choice.
    for text in ["think hard about the trade-offs and debug the root cause, "
                 "step by step, why is it failing"]:
        assert reasoning.auto_level(text) != "high"


def test_explicit_level_beats_auto():
    level, _cfg = reasoning.effective_level("hi", {"reasoning": {"level": "high"}})
    assert level == "high", level
    # An override beats everything, including a configured level.
    level, _cfg = reasoning.effective_level("hi", {"reasoning": {"level": "high"}},
                                            override="off")
    assert level == "off", level


def test_thinking_skipped_on_middle_rounds():
    """The core token saving: rounds 1..n-1 of a tool loop must not think."""
    assert reasoning.should_think("high", 0) is True
    assert reasoning.should_think("high", 1, is_final=False, ran_tools=True) is False
    assert reasoning.should_think("high", 3, is_final=False, ran_tools=True) is False
    assert reasoning.should_think("high", 4, is_final=True, ran_tools=True) is True
    # A turn that never ran a tool already thought on round 0.
    assert reasoning.should_think("high", 2, is_final=True, ran_tools=False) is False
    assert reasoning.should_think("off", 0) is False


def test_round_patch_shapes_per_provider():
    p = reasoning.round_patch("anthropic", "medium", 0)
    assert p["thinking"]["budget_tokens"] == 4096, p
    p = reasoning.round_patch("gemini", "medium", 0)
    assert p["_generationConfig"]["thinkingConfig"]["thinkingBudget"] == 4096, p
    p = reasoning.round_patch("openai_compatible", "high", 0, provider_name="openai")
    assert p == {"reasoning_effort": "high"}, p
    p = reasoning.round_patch("openai_compatible", "high", 0, provider_name="openrouter")
    assert p == {"reasoning": {"effort": "high"}}, p
    # A middle round is empty for every provider.
    for kind in ("anthropic", "gemini", "openai_compatible", "ollama"):
        assert reasoning.round_patch(kind, "high", 2, ran_tools=True) == {}, kind


def test_spend_estimate_saves_most_of_it():
    est = reasoning.spend_estimate("high", rounds=5)
    assert est["actual"] < est["naive"], est
    assert est["saved_percent"] >= 60, est


def test_max_tokens_raised_not_budget_shrunk():
    """A 700-token cap with a 12k budget must raise the cap, not silently
    buy no thinking."""
    assert reasoning.fit_max_tokens(700, "high") > 12288
    assert reasoning.fit_max_tokens(700, "off") == 700


def test_thinking_keys_stripped_for_retry():
    payload = {"model": "x", "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert reasoning.strip_from_payload(payload) is True
    assert "thinking" not in payload and "reasoning_effort" not in payload
    assert reasoning.strip_from_payload({"model": "x"}) is False


def test_rejection_detection():
    assert reasoning.looks_like_thinking_rejected(
        "400: property 'reasoning_effort' is unsupported")
    assert reasoning.looks_like_thinking_rejected(
        "Invalid request: thinking is not supported for this model")
    assert not reasoning.looks_like_thinking_rejected("429 rate limited")


def test_trace_extraction_per_provider():
    assert reasoning.extract_trace("anthropic", {
        "content": [{"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "answer"}]}) == "hmm"
    assert reasoning.extract_trace("gemini", {
        "candidates": [{"content": {"parts": [
            {"thought": True, "text": "pondering"}, {"text": "answer"}]}}]}) == "pondering"
    assert reasoning.extract_trace("openai_compatible", {
        "choices": [{"message": {"reasoning_content": "deepseek style"}}]}) == "deepseek style"
    assert reasoning.extract_trace("anthropic", {}) == ""
    assert reasoning.extract_trace("anthropic", None) == ""


# ---------------------------------------------------------------------------
# turn_trace
# ---------------------------------------------------------------------------


class _FakeRoute:
    groups = ["playnite"]
    matches = [("playnite", "playnite_launch_game", "launch")]
    confident = True


def test_trace_renders_plain_language():
    trace = turn_trace.TurnTrace()
    trace.note_route(_FakeRoute())
    trace.note_step("playnite_launch_game", {"game": "Hades II"}, {"ok": True})
    trace.provider = "groq"
    lines = trace.lines()
    joined = " ".join(lines)
    assert "launch" in joined, joined
    assert "Launched a game" in joined, joined
    assert "playnite_launch_game" not in joined, "raw tool names shouldn't leak into prose"


def test_trace_records_failures():
    trace = turn_trace.TurnTrace()
    trace.note_route(_FakeRoute())
    trace.note_step("run_command", {"name": "deploy"}, {"error": "not found"})
    text = " ".join(trace.lines())
    assert "didn't work" in text or "not found" in text, text


def test_trace_roundtrips_through_extra():
    trace = turn_trace.TurnTrace()
    trace.note_route(_FakeRoute())
    trace.note_step("web_search", {"query": "x"}, {"ok": True})
    trace.thinking_level = "medium"
    data = trace.to_dict()
    assert json.loads(json.dumps(data)) == data, "trace extra must be JSON-serializable"
    rebuilt = turn_trace.from_extra(data)
    assert rebuilt.lines() == trace.lines()


def test_trace_caps_steps():
    trace = turn_trace.TurnTrace()
    for i in range(100):
        trace.note_step("tool_%d" % i, {}, {"ok": True})
    assert len(trace.steps) <= turn_trace.MAX_STEPS


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def test_same_tool_different_argument_different_risk():
    with _isolated():
        safe = policy.decide("write_file", {"path": "~/Documents/a.md", "content": "hi"})
        bad = policy.decide("write_file", {"path": "~/.ssh/authorized_keys", "content": "k"})
        assert bad["score"] > safe["score"], (safe["score"], bad["score"])
        assert bad["decision"] == policy.DENY, bad


def test_same_call_different_context_different_decision():
    with _isolated():
        interactive = policy.decide("type_text", {"text": "hi"},
                                    context=policy.CTX_INTERACTIVE)
        scheduled = policy.decide("type_text", {"text": "hi"},
                                  context=policy.CTX_SCHEDULED)
        assert scheduled["decision"] == policy.DENY, scheduled
        assert interactive["decision"] != policy.DENY, interactive


def test_readonly_commands_do_not_cry_wolf():
    with _isolated():
        assert policy.decide("run_custom_command", {"command": "git status"})["decision"] \
            == policy.ALLOW
        assert policy.decide("run_custom_command", {"command": "ls -la"})["decision"] \
            == policy.ALLOW
        # ...but a read-only prefix must not launder what follows it.
        sneaky = policy.decide("run_custom_command", {"command": "git status && rm -rf /"})
        assert sneaky["decision"] == policy.DENY, sneaky


def test_policy_can_only_tighten():
    assert policy.escalate(policy.ALLOW, policy.CONFIRM) == policy.CONFIRM
    assert policy.escalate(policy.DENY, policy.ALLOW) == policy.DENY
    with _isolated():
        # A baseline of CONFIRM survives even when the score says allow.
        verdict = policy.decide("get_battery", {}, baseline=policy.CONFIRM)
        assert verdict["decision"] == policy.CONFIRM, verdict


def test_broken_policy_file_fails_closed():
    with _isolated() as jarvis_dir:
        (jarvis_dir / "policy.json").write_text("{not json", encoding="utf-8")
        loaded, problems = policy.load_policy()
        assert problems, "a broken policy file must report a problem"
        assert loaded["enabled"] is True, "defaults must stay in force"
        assert loaded["rules"], "built-in rules must survive a parse failure"


def test_custom_rule_is_honoured():
    with _isolated() as jarvis_dir:
        (jarvis_dir / "policy.json").write_text(json.dumps({
            "rules": [{"when": {"tool": "get_battery"}, "then": "deny",
                       "because": "testing"}]}), encoding="utf-8")
        verdict = policy.decide("get_battery", {})
        assert verdict["decision"] == policy.DENY, verdict
        assert verdict["matched_rule"] == "testing", verdict


def test_unknown_tools_are_not_free():
    """The promise in _base_risk's comment is that an unclassified tool is
    never the cheapest thing in the catalog — not that the heuristic
    recognises every destructive verb in English, which it can't."""
    unknown = policy._base_risk("frobnicate_the_widget")
    readonly = policy._base_risk("get_something")
    assert readonly <= 10, readonly
    assert unknown > readonly, (unknown, readonly)
    assert unknown >= 20, unknown
    # Names that DO advertise what they do are scored accordingly.
    for name in ("wipe_disk", "delete_everything", "obliterate_backups"):
        assert policy._base_risk(name) >= 45, name
    for name in ("write_config", "install_thing"):
        assert policy._base_risk(name) >= 40, name


def test_dry_run_executes_nothing():
    with _isolated():
        out = policy.dry_run("run_custom_command", {"command": "rm -rf /tmp/x"})
        assert out["note"] == "Nothing was executed."
        assert "rm -rf" in out["would"]
        assert out["reversible"] is False


# ---------------------------------------------------------------------------
# timespec recurrence rules
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 9, 17, 14, 30)  # a Thursday


def test_except_clause_does_not_invert_the_schedule():
    """The bug this guards: 'every day except weekends' once matched the
    WEEKEND branch and produced a job that fired only at weekends."""
    trigger = timespec.parse_trigger("every day at 8 except weekends", _NOW)
    assert trigger.get("only_on") != "weekend", trigger
    assert trigger["rule"].get("skip_weekends") is True, trigger


def test_weekday_except_holidays():
    trigger = timespec.parse_trigger("every weekday at 9am except holidays", _NOW)
    assert trigger["only_on"] == "weekday"
    assert trigger["rule"]["skip_holidays"] is True


def test_ordinal_weekday():
    trigger = timespec.parse_trigger("first monday of the month at 10:00", _NOW)
    assert trigger["rule"] == {"ordinal": 1, "weekday": 0}, trigger
    first = timespec.from_iso(trigger["at"])
    assert first.weekday() == 0 and first.day <= 7, first


def test_last_weekday_of_month():
    trigger = timespec.parse_trigger("last friday of the month", _NOW)
    assert trigger["rule"]["ordinal"] == -1


def test_every_other_week_has_a_phase_anchor():
    trigger = timespec.parse_trigger("every other tuesday", _NOW)
    assert trigger["rule"]["every_n_weeks"] == 2, trigger


def test_multiple_named_days():
    trigger = timespec.parse_trigger("every monday and thursday at 18:00", _NOW)
    assert trigger["rule"]["only_days"] == [0, 3], trigger


def test_matches_rule_skips_holidays():
    with _isolated() as jarvis_dir:
        (jarvis_dir / "holidays.json").write_text(json.dumps(["2026-12-25"]),
                                                  encoding="utf-8")
        rule = {"skip_holidays": True}
        assert timespec.matches_rule(datetime(2026, 12, 24, 9, 0), rule) is True
        assert timespec.matches_rule(datetime(2026, 12, 25, 9, 0), rule) is False


def test_missing_holiday_file_fails_open():
    """A reminder that fires on a holiday is an annoyance; one that silently
    never fires is a missed appointment."""
    with _isolated():
        assert timespec.matches_rule(datetime(2026, 12, 25, 9, 0),
                                     {"skip_holidays": True}) is True


def test_existing_triggers_unaffected():
    for text in ["in 20 minutes", "tomorrow at 9am", "every 30 minutes",
                 "every day at 9am", "every monday at 8"]:
        trigger = timespec.parse_trigger(text, _NOW)
        assert trigger.get("rule") is None, (text, trigger)


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


def test_priority_resolution():
    cfg = {"batch_kinds": ["task"], "enabled": True}
    assert digest.normalize_priority(None, "task", cfg) == "low"
    assert digest.normalize_priority(None, "reminder", cfg) == "normal"
    assert digest.normalize_priority("urgent") == "high"
    # An explicit priority always beats the per-kind default.
    assert digest.normalize_priority("high", "task", cfg) == "high"


def test_only_low_batches_and_only_when_enabled():
    assert digest.should_batch("low", {"enabled": True}) is True
    assert digest.should_batch("normal", {"enabled": True}) is False
    assert digest.should_batch("low", {"enabled": False}) is False


def test_summary_collapses_repeats_and_surfaces_failures():
    base = datetime(2026, 9, 17, 8, 0)
    items = [{"title": "Nightly backup", "message": "ok", "failed": False,
              "at": (base - timedelta(days=i)).isoformat()} for i in range(7)]
    items.append({"title": "Build", "message": "exit 1", "failed": True,
                  "at": base.isoformat()})
    summary = digest.build_summary(items, now=base)
    assert "\u00d77" in summary, summary
    assert summary.index("Build") < summary.index("Nightly backup"), \
        "failures must come first"


def test_queue_survives_a_failed_send():
    with _isolated():
        digest.save_config({"enabled": True})
        digest.enqueue({"title": "x", "message": "y", "kind": "task"})
        assert digest.pending_count() == 1
        result = digest.flush(force=True, dry_run=True)
        assert result["sent"] is False
        assert digest.pending_count() == 1, "a dry run must not clear the queue"


# ---------------------------------------------------------------------------
# memory_semantic
# ---------------------------------------------------------------------------


_FACTS = [
    {"id": "a", "key": "theme_pref", "fact": "Prefers dark mode in every app", "tags": ["prefs"]},
    {"id": "b", "key": "main_pc", "fact": "Main PC has an RTX 4080", "tags": ["hardware"]},
    {"id": "c", "key": "deploy_day", "fact": "Deploys go out Thursdays", "tags": ["process"]},
]


def test_semantic_finds_paraphrase():
    with _isolated():
        hits = memory_semantic.rank("do I like light themes?", _FACTS)
        assert hits, "a paraphrase with no shared words should still match"
        assert hits[0][1]["id"] == "a", hits


def test_semantic_returns_nothing_for_unrelated():
    with _isolated():
        hits = memory_semantic.rank("what is the capital of France", _FACTS)
        assert not hits or hits[0][0] < 0.3, hits


def test_cosine_and_vectorize_are_sane():
    v1 = memory_semantic.vectorize("dark mode preference")
    v2 = memory_semantic.vectorize("dark mode preference")
    assert abs(memory_semantic.cosine(v1, v2) - 1.0) < 0.01
    assert memory_semantic.cosine(v1, {}) == 0.0
    assert memory_semantic.vectorize("") == {}


def test_index_rebuilds_when_facts_change():
    with _isolated():
        index = memory_semantic.get_index(_FACTS)
        changed = _FACTS + [{"id": "d", "fact": "New fact", "tags": []}]
        rebuilt = memory_semantic.get_index(changed)
        assert rebuilt["hash"] != index["hash"]
        assert "d" in rebuilt["vectors"]


def test_boost_is_bounded():
    with _isolated():
        for value in memory_semantic.boost_map("dark mode", _FACTS).values():
            assert value <= memory_semantic.SEMANTIC_WEIGHT


# ---------------------------------------------------------------------------
# ui_bridge — the headless rule is the one that matters
# ---------------------------------------------------------------------------


class _ExplodingStdin:
    def isatty(self):
        return False

    def readline(self):
        raise AssertionError("a headless prompt must never read stdin")


def test_headless_prompts_return_defaults_without_blocking():
    saved_stdin, saved_ui = sys.stdin, os.environ.pop("JARVIS_UI", None)
    sys.stdin = _ExplodingStdin()
    try:
        assert ui_bridge.confirm("delete?", default=False) is False
        assert ui_bridge.confirm("keep?", default=True) is True
        assert ui_bridge.choose("env?", ["a", "b"], default="a") == "a"
        assert ui_bridge.prompt("name?", default="x") == "x"
        assert ui_bridge.form("s", [{"name": "h", "default": "1"}]) == {"h": "1"}
    finally:
        sys.stdin = saved_stdin
        if saved_ui is not None:
            os.environ["JARVIS_UI"] = saved_ui


def test_level_validation_and_clipping():
    assert ui_bridge._normalize_level("warning") == "warn"
    assert ui_bridge._normalize_level("<script>") == "info"
    long_text = "x" * 9000
    assert len(ui_bridge._clip(long_text, ui_bridge.MAX_BODY)) <= ui_bridge.MAX_BODY


def test_emit_never_raises_on_unserializable():
    event = ui_bridge.emit("toast", message=object())
    assert event["kind"] in ("toast",)


# ---------------------------------------------------------------------------
# custom_tools_store
# ---------------------------------------------------------------------------


_GOOD = '''
def tool_hello(args):
    return {"ok": True}

TOOL_SCHEMAS = [{"name": "hello", "description": "Greet.",
                 "parameters": {"type": "object", "properties": {}}}]
TOOLS = {"hello": tool_hello}
TOOL_GROUP = "custom"
'''


def test_every_template_validates():
    with _isolated():
        for entry in custom_tools_store.templates():
            source = custom_tools_store.template_source(entry["id"])
            result = custom_tools_store.validate_source(source, entry["id"])
            assert result["ok"], (entry["id"], result.get("error"))


def test_error_stages_are_distinguishable():
    with _isolated():
        assert custom_tools_store.validate_source("def x(:", "t")["stage"] == "syntax"
        assert custom_tools_store.validate_source(
            "TOOL_SCHEMAS=[]\nTOOLS={}\nTOOL_GROUP='x'", "t")["stage"] == "contract"
        assert custom_tools_store.validate_source("", "t")["stage"] == "empty"


def test_saved_tool_does_not_clash_with_itself():
    """Regression: a saved tool is in the live catalog, so a naive clash
    check found its own name and reported the file as permanently broken."""
    with _isolated():
        assert custom_tools_store.write_tool("hello_tool", _GOOD)["ok"]
        again = custom_tools_store.validate_source(_GOOD, "hello_tool")
        assert again["ok"], again.get("error")


def test_enable_disable_roundtrip():
    with _isolated():
        custom_tools_store.write_tool("toggle_me", _GOOD)
        assert custom_tools_store.set_enabled("toggle_me", False)["enabled"] is False
        listed = {t["name"]: t for t in custom_tools_store.list_tools()}
        assert listed["toggle_me"]["enabled"] is False
        assert custom_tools_store.set_enabled("toggle_me", True)["enabled"] is True


def test_bad_names_rejected():
    with _isolated():
        for name in ("Bad Name", "9lives", "", "x" * 60):
            assert custom_tools_store.write_tool(name, _GOOD)["ok"] is False, name


# ---------------------------------------------------------------------------
# conv_export
# ---------------------------------------------------------------------------


def _record():
    return {
        "id": "a" * 16, "title": "Test chat", "soft_context": "a gist",
        "created_at": "2026-09-17T10:00:00",
        "exchanges": [
            {"ts": "2026-09-17T10:00:00", "user": "hello", "jarvis": "hi",
             "provider": "groq",
             "extras": [{"type": "thinking", "data": {"text": "pondering", "level": "low"}},
                        {"type": "trace", "data": {"lines": ["Did a thing."],
                                                   "steps": [{"name": "web_search",
                                                              "detail": "x"}]}}]},
        ],
    }


def test_markdown_excludes_machinery_by_default():
    text = conv_export.render_markdown(_record())
    assert "hello" in text and "hi" in text
    assert "pondering" not in text, "thinking must be opt-in"
    assert "web_search" not in text, "tool calls must be opt-in"


def test_markdown_includes_machinery_when_asked():
    text = conv_export.render_markdown(_record(), include_thinking=True,
                                       include_tools=True, include_trace=True)
    assert "pondering" in text and "web_search" in text and "Did a thing." in text


def test_html_escapes_user_content():
    record = _record()
    record["exchanges"][0]["user"] = "<script>alert(1)</script>"
    html_text = conv_export.render_html(record)
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;" in html_text


def test_export_rejects_bad_input():
    assert conv_export.export("nope", "md")["ok"] is False
    assert conv_export.export("a" * 16, "docx")["ok"] is False


# ---------------------------------------------------------------------------


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        check(name, fn)

    print("\n%d passed, %d failed  (%d total)\n"
          % (len(PASSED), len(FAILED), len(tests)))
    if FAILED:
        for name, why in FAILED:
            print("  FAIL  %s\n        %s" % (name, why))
        return 1
    print("  all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
