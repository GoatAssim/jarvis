"""Regression tests for D.5-#1 and D.5-#4 (status audit, rev. 2026-09-26).

D.5-#1 — a stale, irrelevant memory fact (in the sampled logs: a fact keyed
`jarvis_project_dir`) was injected into "Relevant long-term memory" on
essentially every request, because:
  (a) the user's message almost always opens with the assistant's own wake
      word ("jarvis, could you..."), which memory._score_fact's key/tag
      overlap bonus treated as a real relevance signal whenever a fact's
      key or tags happened to contain that word, and
  (b) independently, memory_semantic's n-gram similarity gave the same
      fact an outsized boost against ANY message containing the wake word,
      because the fact's own text repeats it (a path containing
      "...\\jarvis\\jarvis v2\\jarvis").
Both are closed by stripping the wake word from the query before it
reaches either scorer (memory.prompt_context's `assistant_name` param).

D.5-#4 — after a tool call was correctly denied (`{"error": "tool not
permitted"}`), the model sometimes narrated success anyway ("As you
wish."). The existing honesty instruction only covered claiming an action
with NO tool call at all; it said nothing about a tool call that ran and
came back with an error. All three persona instruction variants now say
so explicitly.

No network, no live model — this only checks the prompt-construction
functions in jarvis/memory.py and jarvis/ai_client.py.

Run: python3 tests/test_d5_fixes.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, memory  # noqa: E402

# A small, realistic multi-fact corpus. Deliberately more than one fact so
# memory_semantic's relative (median/top-fraction) thresholding behaves as
# it does in production — a single-fact corpus trivially "qualifies" any
# nonzero similarity and would hide the semantic half of this bug.
_PROJECT_DIR_FACT = {
    "id": "m_779b565b",
    "key": "jarvis_project_dir",
    "fact": (
        "Jarvis main project directory is located at "
        "D:\\MyDigitalVault\\prjects\\whole jarvis\\jarvis v2\\jarvis"
    ),
    "tags": [],
}
_CORPUS = [
    {"id": "m_1", "key": "preferred_name", "fact": "Call me Assim.", "tags": ["identity"]},
    {"id": "m_2", "key": "main_pc", "fact": "Main PC is a Ryzen 7800X3D with an RTX 4080.",
     "tags": ["hardware"]},
    {"id": "m_3", "key": "favorite_genre", "fact": "Prefers dark mode in every app.",
     "tags": ["prefs"]},
    {"id": "m_4", "key": "wifi_ssid", "fact": "Home wifi SSID is Nakama_5G.", "tags": ["network"]},
    _PROJECT_DIR_FACT,
    {"id": "m_6", "key": "favorite_game", "fact": "Big fan of Elden Ring, plays it most weekends.",
     "tags": ["games"]},
    {"id": "m_7", "key": "work_deploy", "fact": "Deploys go out on Thursdays for the ratioty project.",
     "tags": ["work"]},
    {"id": "m_8", "key": "timezone", "fact": "Lives in the GMT+1 timezone.", "tags": ["identity"]},
]

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _with_corpus(fn):
    """Run fn() with memory.load_facts() swapped for our fixed corpus, then
    restore it — plain monkeypatch, no test framework needed here."""
    original = memory.load_facts
    memory.load_facts = lambda: list(_CORPUS)
    try:
        return fn()
    finally:
        memory.load_facts = original


def _project_dir_injected(query, assistant_name="J.A.R.V.I.S"):
    out = memory.prompt_context(query=query, assistant_name=assistant_name)
    return "jarvis_project_dir" in out


# --- D.5-#1: wake word must not fake relevance ------------------------------

def test_wake_word_alone_does_not_inject_unrelated_fact():
    for msg in (
        "jarvis how are you doing today",
        "hey jarvis lol whats up",
        "good morning jarvis",
        "jarvis tell me a joke",
        "jarvis whats my wifi password",
        "jarvis could you move this pdf to my documents folder",
    ):
        hit = _with_corpus(lambda m=msg: _project_dir_injected(m))
        check(f"not injected on unrelated msg: {msg!r}", not hit,
              "fact still appeared with only the wake word in common")


def test_on_topic_query_still_surfaces_the_fact():
    # The fix must not blind relevance scoring to this fact entirely —
    # only stop the wake word alone from being read as topical relevance.
    for msg in (
        "jarvis what is my project directory again",
        "hey jarvis help me out, D:\\MyDigitalVault\\prjects\\ratioty\\main.py token issue",
    ):
        hit = _with_corpus(lambda m=msg: _project_dir_injected(m))
        check(f"still surfaced on on-topic msg: {msg!r}", hit,
              "genuinely relevant query no longer surfaces the fact")


def test_no_assistant_name_is_a_safe_no_op():
    # Callers that don't pass assistant_name (or pass None) must fall back
    # to the exact prior behavior, not raise or silently misbehave.
    hit = _with_corpus(lambda: _project_dir_injected(
        "jarvis what is my project directory again", assistant_name=None))
    check("assistant_name=None still surfaces an on-topic match", hit)


def test_dotted_default_name_tokenizes_correctly():
    # DEFAULT_ASSISTANT_NAME is "J.A.R.V.I.S" — single letters separated by
    # dots. memory._WORD_RE needs runs of 2+ alnum chars, so the wake-word
    # stripping must remove the dots first or it silently strips nothing.
    hit = _with_corpus(lambda: _project_dir_injected(
        "jarvis how are you doing today", assistant_name=ai_client.DEFAULT_ASSISTANT_NAME))
    check("dotted DEFAULT_ASSISTANT_NAME still strips the wake word", not hit)


def test_ai_client_passes_assistant_name_through():
    # Guards against the plumbing (D.5-#1's call site in _build_messages)
    # silently regressing back to not passing assistant_name at all.
    import inspect
    src = inspect.getsource(ai_client._build_messages)
    check("_build_messages passes assistant_name to memory.prompt_context",
          "assistant_name=" in src)


# --- D.5-#4: tool error/denial must never be narrated as success -----------

def _persona_static_prefix(compact_persona, ultra):
    static, _tail = ai_client._system_prompt_parts(
        persona={}, commands_ctx="", freq_ctx="", tools_enabled=False,
        compact_persona=compact_persona, ultra=ultra,
    )
    return static


def test_honesty_instruction_covers_tool_errors_in_every_mode():
    variants = {
        "full": _persona_static_prefix(compact_persona=False, ultra=False),
        "compact": _persona_static_prefix(compact_persona=True, ultra=False),
        "ultra": _persona_static_prefix(compact_persona=True, ultra=True),
    }
    for label, text in variants.items():
        has_old_clause = (
            "unless a tool confirmed it" in text
            or "didn't actually take" in text
        )
        mentions_error_or_denial = (
            "error" in text.lower() or "denial" in text.lower()
            or "denied" in text.lower() or "not permitted" in text.lower()
        )
        check(f"{label} persona: still has the base honesty clause", has_old_clause)
        check(f"{label} persona: now also covers a failed/denied tool result",
              mentions_error_or_denial,
              "a tool_result with an error can still be narrated as success")


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    failures = []
    for test in _TESTS:
        before = len(FAIL)
        try:
            test()
        except AssertionError as e:
            failures.append((test.__name__, str(e)))
            continue
        if len(FAIL) > before:
            failures.append((test.__name__, "see FAILED lines above"))
    total_checks = len(PASS) + len(FAIL)
    print(f"\n{len(PASS)}/{total_checks} checks passed across "
          f"{len(_TESTS) - len(failures)}/{len(_TESTS)} tests")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
