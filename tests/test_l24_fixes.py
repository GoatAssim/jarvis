"""L.24 T2 / T3 / T4 / T7: catalog-tier gate, batch + promoting get_tool_schema,
Gemini dict-result compaction, interactive sensing hint, failover note.

Standalone, plain-assert, like the rest of tests/. Run directly:
    python3 tests/test_l24_fixes.py
"""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import (  # noqa: E402
    ai_client, ai_providers, token_usage, tool_loader, tool_registry,
)
from jarvis import tools as system_tools  # noqa: E402

tool_loader.discover_actions()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _fr_message(name, response):
    return {"role": "user", "parts": [{"functionResponse": {"name": name, "response": response}}]}


# ---- T7: Gemini dict results are compacted like string ones ---------------

def test_gemini_dict_results_are_compacted():
    big = {"windows": [{"title": "Window %02d — %s" % (i, "x" * 40), "pid": i} for i in range(40)]}
    original = copy.deepcopy(big)
    working = [
        _fr_message("list_windows", big),
        _fr_message("list_windows", {"windows": [{"title": "b" * 900}]}),
        _fr_message("read_file", {"content": "c" * 900}),
        _fr_message("list_windows", {"windows": [{"title": "d" * 900}]}),
    ]
    reclaimed = ai_providers._compact_prior_tool_results(working)
    first = working[0]["parts"][0]["functionResponse"]["response"]
    check("an old dict-shaped Gemini result is trimmed", reclaimed > 0 and isinstance(first.get("result"), str)
          and first["result"].endswith(ai_providers._TRIM_MARKER), str(first)[:120])
    check("the trimmed result stays a dict (Gemini needs a Struct)", isinstance(first, dict))
    check("the trimmed result is near the budget",
          len(first["result"]) <= ai_providers.PRIOR_RESULT_CHARS + len(ai_providers._TRIM_MARKER))
    check("the executor's cached dict is NOT mutated (failover replays it)", big == original)
    newest = working[-1]["parts"][0]["functionResponse"]["response"]
    check("the newest results stay untouched", newest == {"windows": [{"title": "d" * 900}]})
    again = ai_providers._compact_prior_tool_results(working)
    check("compaction is idempotent for dict results", again == 0, again)
    small = [_fr_message("a", {"ok": True}), _fr_message("b", {"ok": True}), _fr_message("c", {"ok": True})]
    ai_providers._compact_prior_tool_results(small)
    check("a small dict result is left alone", small[0]["parts"][0]["functionResponse"]["response"] == {"ok": True})


# ---- T2: net-saving gate --------------------------------------------------

def _saving(group):
    names = tool_registry.tools_in_group(group)
    sch = system_tools.schemas_for_tools(names)
    return (token_usage.estimate_tokens_for(system_tools.compact_schemas_for_prompt(sch))
            - token_usage.estimate_tokens_for(system_tools.catalog_schemas_for_prompt(sch)))


def test_catalog_gate_matches_measured_savings():
    floor = ai_client.CATALOG_TIER_MIN_SAVING_TOKENS
    check("playnite still clears the demotion bar", _saving("playnite") >= floor, _saving("playnite"))
    for group in ("desktop", "files", "system_control", "workspace"):
        check(f"{group} is too cheap to demote (saving < {floor})", _saving(group) < floor, _saving(group))
    check("the 150% mode still opts out of the tier entirely",
          ai_client._MODE_BY_NAME["precise"].get("catalog_tier") is False)


# ---- T2: OrderedSchemaSet.replace ----------------------------------------

def test_replace_keeps_position():
    s = ai_client.OrderedSchemaSet([{"name": "a", "v": 1}, {"name": "b", "v": 1}, {"name": "c", "v": 1}])
    s.replace({"name": "b", "v": 2})
    check("replace swaps the entry in place", [x["name"] for x in s] == ["a", "b", "c"] and s.to_list()[1]["v"] == 2)
    s.append({"name": "b", "v": 3})
    check("append is still first-wins", s.to_list()[1]["v"] == 2)
    s.replace({"name": "z", "v": 1})
    check("replace of an unknown name adds it", "z" in s)


# ---- T2: get_tool_schema list form ---------------------------------------

def test_get_tool_schema_batch():
    one = system_tools.tool_get_tool_schema({"name": "playnite_launch_game"})
    check("single-name form is unchanged", one.get("ready") is True and "schema" in one and one["name"] == "playnite_launch_game")
    got = system_tools.tool_get_tool_schema({"names": ["playnite_launch_game", "playnite_search", "no_such_tool_xyz"]})
    names = [x["name"] for x in got.get("schemas", [])]
    check("list form returns every real schema", "playnite_launch_game" in names, got)
    check("list form reports the bad name instead of failing the call",
          any(e.get("name") == "no_such_tool_xyz" for e in got.get("errors", [])), got)
    many = system_tools.tool_get_tool_schema({"names": [f"playnite_{i}" for i in range(10)]})
    check("the list is capped", len(many.get("schemas", [])) + len(many.get("errors", [])) <= system_tools.GET_TOOL_SCHEMA_MAX_NAMES
          and len(many.get("not_fetched", [])) == 10 - system_tools.GET_TOOL_SCHEMA_MAX_NAMES, many)
    check("no name at all is still an error", "error" in system_tools.tool_get_tool_schema({}))
    check("name + names are merged without duplicates",
          len(system_tools.tool_get_tool_schema({"name": "playnite_launch_game",
                                                  "names": ["playnite_launch_game", "playnite_search"]})["schemas"]) <= 2)
    decl = next(s for s in system_tools.DISCOVERY_TOOL_SCHEMAS if s["name"] == "get_tool_schema")
    check("the declaration offers `names` and no longer requires `name`",
          "names" in decl["parameters"]["properties"] and "name" not in decl["parameters"]["required"])


# ---- T2: a get_tool_schema result promotes exactly the fetched tools -----

def test_executor_promotes_fetched_tools():
    promoted, discovered = [], []

    def sink(names):
        discovered.extend(names)

    sink.promote_exact = lambda names: promoted.extend(names)
    ex = ai_client._make_tool_executor(lambda *a, **k: None, schemas=[], discover_sink=sink)
    ex("get_tool_schema", {"name": "playnite_launch_game"})
    check("a single fetch is promoted by exact name", promoted == ["playnite_launch_game"], promoted)
    check("...and does NOT activate the whole group", discovered == [], discovered)
    promoted.clear()
    ex("get_tool_schema", {"names": ["playnite_launch_game", "playnite_search"]})
    check("a list fetch promotes every schema returned", "playnite_launch_game" in promoted, promoted)
    bad = []
    sink2 = lambda names: None  # noqa: E731 — a double with no promote_exact must be tolerated
    ex2 = ai_client._make_tool_executor(lambda *a, **k: None, schemas=[], discover_sink=sink2)
    try:
        ex2("get_tool_schema", {"name": "playnite_launch_game"})
    except Exception as e:  # noqa: BLE001
        bad.append(e)
    check("a sink without promote_exact is left alone", not bad, bad)


# ---- T3: interactive sensing hint ----------------------------------------

def test_sensing_failure_tells_the_model_not_to_substitute():
    from jarvis import ocr_tools
    out = ocr_tools._with_vision_failure({"error": "OCR unavailable."}, "no vision")
    check("the failure stays non-retryable", out.get("retryable") is False and out.get("can_see_screen") is False)
    hint = out.get("hint", "")
    check("it names the screenshot/list_windows fallback and says not to", "take a screenshot" in hint
          and "list" in hint and "Do not" in hint, hint)


# ---- T4: failover note ----------------------------------------------------

def test_failover_note_says_do_not_redo():
    runs = [{"name": "launch_app", "arguments": {"name": "x"}, "result": {"ok": True}}]
    note = ai_client._tool_runs_note(runs, 2000)
    mutated = [r for r in runs if ai_client._ran_something_real(r["name"])]
    if mutated:
        check("a failover note says not to run completed actions again", "Do not run them again" in note, note[:200])
    else:
        check("a failover note keeps the read-only reuse line", "do not repeat" in note, note[:200])


for fn in [
    test_gemini_dict_results_are_compacted,
    test_catalog_gate_matches_measured_savings,
    test_replace_keeps_position,
    test_get_tool_schema_batch,
    test_executor_promotes_fetched_tools,
    test_sensing_failure_tells_the_model_not_to_substitute,
    test_failover_note_says_do_not_redo,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
