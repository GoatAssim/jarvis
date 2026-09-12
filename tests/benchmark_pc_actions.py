#!/usr/bin/env python3
"""benchmark_pc_actions.py — fixed 4-step token/tool-call benchmark.

Drop this file into `tests/` (next to interactive_inspector.py) in EITHER
jarvis-main or jarvis-counterreword and run it from the repo root with:

    python3 tests/benchmark_pc_actions.py

It runs one fixed conversation — quick web search, "go on Discord and tag
@no and say hi", "open the TTS bot", "launch Roblox" — through the real
`jarvis.ai_client.ask()` for whichever repo it's run from, using your real
configured provider (same ai_config.json either repo already reads), and
prints + saves a full breakdown of every token this cost. Run it once in
each repo (same provider/model in both configs, for an apples-to-apples
number) and feed both JSON result files to compare_benchmark_results.py
for a side-by-side delta.

MEASUREMENT — this reads AskResult.usage directly rather than
instrumenting anything itself. Both repos already carry the same "Phase 0"
usage-tracking plumbing (see new_plan.md, token_usage.py, and
ai_providers.py's _record_usage/_call_tool_safely/get_usage_summary): every
adapter reports REAL provider-reported prompt/completion tokens per
request round via _record_usage, and every tool call gets a local
~estimate (token_usage.estimate_tokens_for — the same ~4-chars/token
heuristic reused here for the reply text) via _call_tool_safely, all
rolled up into result.usage = {input_tokens, output_tokens, total_tokens,
rounds: [...], tool_calls: [...]}. Reading that instead of re-measuring
independently means this benchmark's numbers are exactly what each repo's
own Logs/debug panel would already show for the same conversation — no
separate parsing logic to keep in sync with either codebase, and no risk
of silently under- or over-counting relative to what's actually billed.
(An earlier draft of this script instead shadow-metered every HTTP
request itself, on the mistaken assumption that this usage plumbing was
counterreword-only — a stale, already-superseded .rej in that repo's zip
briefly suggested that. It isn't: both repos have it, so reading it
directly is both simpler and more faithful than re-deriving it.)

CAVEAT — if your configured provider's API doesn't return a usage block at
all (some Ollama setups, some misconfigured OpenAI-compatible endpoints),
result.usage's `rounds` list comes back empty for that ask even though
real requests happened — token_usage.extract_usage() only appends a round
when the response actually carried one. This script flags that plainly in
the report rather than silently reporting 0; if you see the warning,
either the answer came from a provider that doesn't report usage, or
something's misconfigured, not a "zero-cost" ask.

SAFETY — nothing here launches Discord, opens an app, or launches Roblox.
Every tool call gets routed through _metered_execute_tool() below, which
only ever lets a tool marked read-only (see is_safe_tool()) actually run
(get_window_info, list_windows, search_files, web_search, ...). Everything
else — run_command, playnite_launch_game, click_on_text, type_text,
hotkey, focus_window, open_file, and every other tool with a real
side-effect — gets intercepted and handed back a canned
{"ok": true, "simulated": true, ...} response instead of actually running,
so the model can proceed through the conversation as if each step
"worked" without anything actually happening on this machine. This is a
default-deny allowlist (only known-read-only tools run for real), not a
denylist of specific dangerous ones, specifically so a tool this script's
author didn't think of still gets stubbed rather than silently executed.
The stub still flows through the exact same _call_tool_safely accounting
as a real result would, so its (smaller) token cost is still captured
accurately in result.usage — stubbing changes what happens, not whether
it gets counted.
"""

import json
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_config, ai_client, conversations, token_usage  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Safety: default-deny allowlist of tools that never touch real app/OS
# state and are therefore safe to actually run during the benchmark.
# Everything NOT in here gets stubbed — see module docstring.
# ---------------------------------------------------------------------------
SAFE_READ_ONLY_PREFIXES = ("get_", "list_", "search_")
SAFE_READ_ONLY_TOOLS = {
    "web_search", "web_fetch",
    "radio_status",
    "spotify_now", "spotify_playlists", "spotify_search", "spotify_suggest",
    "package_info", "package_list", "package_managers", "package_search",
    "playnite_app_info", "playnite_get_achievements", "playnite_get_activity",
    "playnite_get_cover", "playnite_get_game", "playnite_get_skill",
    "playnite_library_stats", "playnite_list_addons", "playnite_list_collections",
    "playnite_list_frequent", "playnite_list_game_actions", "playnite_list_missing_art",
    "playnite_list_plugins", "playnite_query_games", "playnite_view",
    "memory_search",
    "ytdl_info", "ytdl_formats",
}


def is_safe_tool(name):
    if name in SAFE_READ_ONLY_TOOLS:
        return True
    return isinstance(name, str) and name.startswith(SAFE_READ_ONLY_PREFIXES)


def _generic_stub_result(name, arguments):
    return {
        "ok": True,
        "simulated": True,
        "note": (
            f"[benchmark] {name} was NOT actually run \u2014 this is a canned "
            "success response so the benchmark never touches real app/OS state."
        ),
    }


def install_stub(stub_calls):
    """Monkeypatches jarvis.tools.execute_tool in place so any tool
    outside SAFE_READ_ONLY_TOOLS gets a canned success response instead of
    actually running, recording (name, stubbed) into `stub_calls` so the
    report can show how many of each. Returns a restore() callable."""
    real_execute_tool = system_tools.execute_tool

    def metered_execute_tool(name, arguments=None, verbosity=None):
        arguments = arguments or {}
        stubbed = not is_safe_tool(name)
        stub_calls.append({"name": name, "stubbed": stubbed})
        if stubbed:
            return _generic_stub_result(name, arguments)
        return real_execute_tool(name, arguments, verbosity=verbosity)

    system_tools.execute_tool = metered_execute_tool

    def restore():
        system_tools.execute_tool = real_execute_tool

    return restore


# ---------------------------------------------------------------------------
# The fixed scenario.
# ---------------------------------------------------------------------------
STEPS = [
    "Do a quick web search for today's top tech news.",
    "Go on Discord, tag @no and say hi.",
    "Open the TTS bot.",
    "Launch Roblox.",
]


def _empty_usage():
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "rounds": [], "tool_calls": []}


def run_benchmark(label):
    cfg = ai_config.load_ai_config()
    providers = [p for p in cfg.get("providers", []) if p.get("enabled")]
    if not providers:
        print("No enabled provider configured (see `jarvis ai-config`) \u2014 "
              "can't run a live benchmark without one to actually talk to.",
              file=sys.stderr)
        sys.exit(1)

    conv_id = conversations.new_conversation(title="[benchmark] pc actions", make_current=False)
    steps_out = []

    for i, prompt in enumerate(STEPS, start=1):
        stub_calls = []
        restore = install_stub(stub_calls)
        print(f"\n--- step {i}/{len(STEPS)}: {prompt}", file=sys.stderr)
        t0 = time.monotonic()
        try:
            result = ai_client.ask(prompt, conversation_id=conv_id)
        finally:
            restore()
        elapsed = time.monotonic() - t0

        usage = result.usage or _empty_usage()
        rounds = usage.get("rounds") or []
        tool_calls_usage = usage.get("tool_calls") or []
        no_reported_usage = not rounds and bool(stub_calls or result.ok)

        print(f"    -> {'ok' if result.ok else 'FAILED'} "
              f"({elapsed:.1f}s, provider={result.provider}, "
              f"{len(stub_calls)} tool call(s), {len(rounds)} request round(s))",
              file=sys.stderr)
        if no_reported_usage:
            print("    !! provider reported no usage block for this ask \u2014 "
                  "token totals below will be incomplete/estimated only.", file=sys.stderr)

        steps_out.append({
            "step": i,
            "prompt": prompt,
            "ok": result.ok,
            "provider": result.provider,
            "reply": result.text,
            "reply_tokens_estimated": token_usage.estimate_tokens_for(result.text),
            "elapsed_s": round(elapsed, 2),
            "tool_call_count": len(stub_calls),
            "tool_call_count_real": sum(1 for c in stub_calls if not c["stubbed"]),
            "tool_call_count_stubbed": sum(1 for c in stub_calls if c["stubbed"]),
            "tool_calls": [c["name"] for c in stub_calls],
            "request_round_count": len(rounds),
            "reported_input_tokens": usage.get("input_tokens", 0),
            "reported_output_tokens": usage.get("output_tokens", 0),
            "reported_total_tokens": usage.get("total_tokens", 0),
            "tool_call_tokens_estimated": sum(
                (t.get("input_tokens") or 0) + (t.get("output_tokens") or 0) for t in tool_calls_usage
            ),
            "no_reported_usage": no_reported_usage,
            "rounds": rounds,
            "tool_call_usage_detail": tool_calls_usage,
        })

    return build_report(label, steps_out)


def build_report(label, steps):
    return {
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": steps,
        "totals": {
            "tool_call_count": sum(s["tool_call_count"] for s in steps),
            "tool_call_count_real": sum(s["tool_call_count_real"] for s in steps),
            "tool_call_count_stubbed": sum(s["tool_call_count_stubbed"] for s in steps),
            "request_round_count": sum(s["request_round_count"] for s in steps),
            "reported_input_tokens": sum(s["reported_input_tokens"] for s in steps),
            "reported_output_tokens": sum(s["reported_output_tokens"] for s in steps),
            "reported_total_tokens": sum(s["reported_total_tokens"] for s in steps),
            "tool_call_tokens_estimated": sum(s["tool_call_tokens_estimated"] for s in steps),
            "reply_tokens_estimated": sum(s["reply_tokens_estimated"] for s in steps),
            "any_step_missing_usage": any(s["no_reported_usage"] for s in steps),
            # The single number most worth comparing across repos.
            # reported_total_tokens already includes every request round's
            # real prompt+completion tokens, which itself already reflects
            # tool-call results resent as context in later rounds — so
            # tool_call_tokens_estimated is added on top only as the
            # "local echo" cost of the call/result pair the moment it's
            # produced, not double-counted against the round totals.
            "grand_total_tokens": (
                sum(s["reported_total_tokens"] for s in steps)
                + sum(s["tool_call_tokens_estimated"] for s in steps)
            ),
        },
    }


def print_report(report):
    t = report["totals"]
    print(f"\n{'=' * 60}")
    print(f"BENCHMARK REPORT \u2014 {report['label']}")
    print(f"{'=' * 60}")
    for s in report["steps"]:
        print(f"\nStep {s['step']}: {s['prompt']}")
        print(f"  ok={s['ok']}  provider={s['provider']}  time={s['elapsed_s']}s")
        print(f"  tool calls: {s['tool_call_count']} "
              f"({s['tool_call_count_real']} real / {s['tool_call_count_stubbed']} stubbed)"
              + (f"  [{', '.join(s['tool_calls'])}]" if s["tool_calls"] else ""))
        print(f"  request rounds: {s['request_round_count']}")
        print(f"  provider-reported tokens: in={s['reported_input_tokens']} "
              f"out={s['reported_output_tokens']} total={s['reported_total_tokens']}")
        print(f"  tool-call tokens (est.): {s['tool_call_tokens_estimated']}")
        if s["no_reported_usage"]:
            print("  !! no usage block reported by the provider for this ask")
        print(f"  reply (est {s['reply_tokens_estimated']} tok): {(s['reply'] or '')[:120]!r}")
    print(f"\n{'-' * 60}")
    print("TOTALS")
    print(f"{'-' * 60}")
    print(f"  request rounds:          {t['request_round_count']}")
    print(f"  tool calls:              {t['tool_call_count']} "
          f"({t['tool_call_count_real']} real / {t['tool_call_count_stubbed']} stubbed)")
    print(f"  provider-reported:       in={t['reported_input_tokens']} out={t['reported_output_tokens']} "
          f"total={t['reported_total_tokens']}")
    print(f"  tool-call tokens (est):  {t['tool_call_tokens_estimated']}")
    print(f"  reply tokens (est):      {t['reply_tokens_estimated']}")
    if t["any_step_missing_usage"]:
        print("  !! one or more steps had no provider-reported usage \u2014 see warnings above")
    print(f"  GRAND TOTAL TOKENS:      {t['grand_total_tokens']}")
    print(f"{'=' * 60}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default=None,
                         help="Name for this run in the output (default: this repo's folder name).")
    parser.add_argument("--out", default=None,
                         help="Output JSON path (default: benchmark_<label>_<timestamp>.json in cwd).")
    args = parser.parse_args()

    label = args.label or Path(__file__).resolve().parent.parent.name
    report = run_benchmark(label)
    print_report(report)

    out_path = Path(args.out) if args.out else Path(
        f"benchmark_{label}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    )
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Full results written to {out_path}")


if __name__ == "__main__":
    main()
