"""Tests for the provider_override knob added to ai_client.ask() /
cli.py's '--provider NAME' flag (see AGENTS.md and the orientation doc).

Same no-framework, plain-assert convention as test_schemas_for_tools.py —
runnable directly:

    python3 tests/test_provider_override.py

These exercise the pure, no-network pieces only: cli._extract_provider_override
(argv parsing) and ai_client._eligible_providers combined with the same
name-matching ask() applies internally — never a live model or API key.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, cli


def _match_override(providers, name):
    """Mirrors the matching ask() does internally once provider_override
    is given, without needing a full ai_config.json / network stack."""
    wanted = name.strip().lower()
    return [p for p in providers if (p.get("name") or "").strip().lower() == wanted]


def test_extract_provider_override_space_form():
    rest, override = cli._extract_provider_override(
        ["--provider", "anthropic", "what", "time", "is", "it"]
    )
    assert rest == ["what", "time", "is", "it"]
    assert override == "anthropic"


def test_extract_provider_override_equals_form():
    rest, override = cli._extract_provider_override(["--provider=groq", "hello"])
    assert rest == ["hello"]
    assert override == "groq"


def test_extract_provider_override_mid_sentence():
    rest, override = cli._extract_provider_override(["hello", "--provider", "openai", "there"])
    assert rest == ["hello", "there"]
    assert override == "openai"


def test_extract_provider_override_absent_is_noop():
    rest, override = cli._extract_provider_override(["hello", "world"])
    assert rest == ["hello", "world"]
    assert override is None


def test_extract_provider_override_trailing_flag_left_alone():
    # No value follows '--provider' -> ambiguous, treated as plain text
    # rather than guessed at.
    rest, override = cli._extract_provider_override(["--provider"])
    assert rest == ["--provider"]
    assert override is None


def test_extract_provider_override_only_first_wins():
    rest, override = cli._extract_provider_override(
        ["--provider", "groq", "--provider", "openai", "hi"]
    )
    assert override == "groq"
    assert rest == ["--provider", "openai", "hi"]


def test_override_matches_case_insensitively():
    providers = ai_client._eligible_providers(
        [{"name": "Groq", "enabled": True, "api_keys": ["k"]}], {}
    )
    assert _match_override(providers, "GROQ") == providers


def test_override_excludes_disabled_provider():
    providers = ai_client._eligible_providers(
        [
            {"name": "openai", "enabled": False, "api_keys": ["k"]},
            {"name": "groq", "enabled": True, "api_keys": ["k"]},
        ],
        {},
    )
    # openai never makes it into the eligible list at all, so an override
    # naming it can't match anything.
    assert _match_override(providers, "openai") == []


def test_override_unknown_name_matches_nothing():
    providers = ai_client._eligible_providers(
        [{"name": "groq", "enabled": True, "api_keys": ["k"]}], {}
    )
    assert _match_override(providers, "made_up_provider") == []


def test_override_ignores_priority_and_isolates_one_provider():
    providers = ai_client._eligible_providers(
        [
            {"name": "gemini", "enabled": True, "api_keys": ["k1"]},
            {"name": "groq", "enabled": True, "api_keys": ["k2"]},
        ],
        {"provider_priority": ["groq", "gemini"]},
    )
    # Without override, priority puts groq first.
    assert [p["name"] for p in providers] == ["groq", "gemini"]
    # With override, only gemini remains, regardless of priority order.
    assert [p["name"] for p in _match_override(providers, "gemini")] == ["gemini"]


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    failures = []
    for test in _TESTS:
        try:
            test()
        except AssertionError as e:
            failures.append((test.__name__, str(e)))
        else:
            print(f"ok       {test.__name__}")
    for name, msg in failures:
        print(f"FAILED   {name}: {msg}")
    print(f"\n{len(_TESTS) - len(failures)}/{len(_TESTS)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
