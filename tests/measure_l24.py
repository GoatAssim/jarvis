"""L.24 measurements, reproducible. Not a test (no test_ prefix, so
run_tests.py skips it): it PRINTS the numbers the T6/T1 work was based on.

    python3 tests/measure_l24.py

1. Per-ask cost vs. what the UI used to show, on the two real logs in
   tests/fixtures (the figure shown was the answering attempt only).
2. Tool-declaration tokens per group, and what the catalog tier saves.
3. What the router offers for the L.24 prompts.

The plan's own logs (5267d91a8b996d48, 1a99e1e3f0d3af0e) are not in the repo,
so section 3 uses their prompts, not their traffic.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

import io  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402

from jarvis import token_usage, tool_loader, tool_registry, tool_router  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    tool_loader.discover_actions()
est = token_usage.estimate_tokens_for


def fixture_rows(name):
    path = ROOT / "tests" / "fixtures" / f"{name}.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]


print("== 1. per-ask cost vs the answering attempt alone ==")
for name in ("81b52bb561796954", "cb140e091ddc66e8"):
    by, order = {}, []
    for r in fixture_rows(name):
        body = (r["data"] or {}).get("body") if r["direction"] == "response" else None
        if not isinstance(body, dict):
            continue
        u = token_usage.extract_usage("gemini" if "usageMetadata" in body else "openai_compatible", body)
        if u:
            by.setdefault(r["provider"], []).append(u)
            if r["provider"] not in order:
                order.append(r["provider"])
    ask = token_usage.AskUsage()
    for i, label in enumerate(order):
        ask.add(label, {"rounds": by[label]}, ok=(i == len(order) - 1))
    t = ask.to_dict()
    last = t["attempts"][-1]
    print(f"{name}: {t['attempt_count']} attempts, {t['rounds']} model calls")
    print(f"   whole ask   : {t['total_tokens']:>6}  (in {t['input_tokens']}, out {t['output_tokens']}, "
          f"thinking {t['thinking_tokens']}, +{t['thinking_in_output_tokens']} reasoning already inside out)")
    print(f"   answering   : {last['total_tokens']:>6}  <- what the UI showed ({100 * last['total_tokens'] / t['total_tokens']:.0f}%)")

print("\n== 2. tool declarations per group (compact schemas, ~tokens) ==")
print(f"{'group':15s}{'tools':>6}{'full':>7}{'catalog-only':>14}")
for g in sorted(tool_registry.TOOL_GROUPS):
    names = tool_registry.TOOL_GROUPS[g]
    sch = system_tools.schemas_for_tools(names)
    print(f"{g:15s}{len(names):>6}{est(system_tools.compact_schemas_for_prompt(sch)):>7}"
          f"{est(system_tools.catalog_schemas_for_prompt(sch)):>14}")

print("\n== 3. what the router offers ==")
for p in ("hey jarvis has anyone texted you on discord", "has anyone texted you", "open discord",
          "Go on Discord, tag @no and say hi.", "Check usage and shutdown PC"):
    r = tool_router.route(p)
    sch = system_tools.schemas_for_tools(r.tools)
    print(f"{p!r}\n   groups={r.groups} tools={len(r.tools)} declarations~{est(system_tools.compact_schemas_for_prompt(sch))} tokens/call")
