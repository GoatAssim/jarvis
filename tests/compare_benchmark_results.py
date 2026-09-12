#!/usr/bin/env python3
"""compare_benchmark_results.py — side-by-side delta between two
benchmark_pc_actions.py result files (one run in jarvis-main, one in
jarvis-counterreword, same provider/model config in both, same STEPS
list — see that script). Not repo-specific itself; just reads the two
JSON files.

Usage:
    python3 tests/compare_benchmark_results.py baseline.json optimized.json
"""

import json
import argparse


def pct_change(old, new):
    if old in (None, 0):
        return None
    return round((new - old) / old * 100, 1)


def fmt_delta(old, new):
    d = new - old
    p = pct_change(old, new)
    sign = "-" if d < 0 else ("+" if d > 0 else "\u00b1")
    tag = " (better)" if d < 0 else (" (worse)" if d > 0 else "")
    pct_s = f", {sign}{abs(p)}%" if p is not None else ""
    return f"{old} \u2192 {new} ({sign}{abs(d)}{pct_s}){tag}"


def print_totals_comparison(base, opt):
    bt, ot = base["totals"], opt["totals"]
    print(f"\n{'=' * 70}")
    print(f"BENCHMARK COMPARISON: {base['label']!r} (baseline) vs {opt['label']!r}")
    print(f"{'=' * 70}\n")

    rows = [
        ("request rounds", "request_round_count"),
        ("tool calls (total)", "tool_call_count"),
        ("tool calls (real)", "tool_call_count_real"),
        ("tool calls (stubbed)", "tool_call_count_stubbed"),
        ("provider-reported input tokens", "reported_input_tokens"),
        ("provider-reported output tokens", "reported_output_tokens"),
        ("provider-reported total tokens", "reported_total_tokens"),
        ("tool-call tokens (est.)", "tool_call_tokens_estimated"),
        ("reply tokens (est.)", "reply_tokens_estimated"),
        ("GRAND TOTAL TOKENS", "grand_total_tokens"),
    ]
    for label, key in rows:
        b, o = bt.get(key, 0), ot.get(key, 0)
        print(f"  {label:<34} {fmt_delta(b, o)}")

    if bt.get("any_step_missing_usage") or ot.get("any_step_missing_usage"):
        print("\n  !! at least one run had a step with no provider-reported usage \u2014 "
              "these totals may be incomplete for that side. See that run's own "
              "printed warnings/JSON for which step.")

    print(f"\n{'-' * 70}")
    print("PER-STEP TOKEN TOTALS (provider-reported total + tool-call tokens est.)")
    print(f"{'-' * 70}")
    base_steps = {s["step"]: s for s in base["steps"]}
    opt_steps = {s["step"]: s for s in opt["steps"]}
    for step_num in sorted(set(base_steps) | set(opt_steps)):
        bs, os_ = base_steps.get(step_num), opt_steps.get(step_num)
        if not bs or not os_:
            print(f"  Step {step_num}: missing from one side, skipping.")
            continue
        b_total = bs["reported_total_tokens"] + bs["tool_call_tokens_estimated"]
        o_total = os_["reported_total_tokens"] + os_["tool_call_tokens_estimated"]
        print(f"  Step {step_num} ({bs['prompt'][:40]!r}...): {fmt_delta(b_total, o_total)}")

    print(f"\n{'=' * 70}")
    overall = pct_change(bt.get("grand_total_tokens") or 0, ot.get("grand_total_tokens") or 0)
    if overall is not None:
        verdict = "an improvement" if overall < 0 else ("a regression" if overall > 0 else "no change")
        print(f"Overall: {opt['label']!r} used {abs(overall)}% "
              f"{'fewer' if overall < 0 else 'more'} total tokens than {base['label']!r} \u2014 {verdict}.")
    print(f"{'=' * 70}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", help="Result JSON from the baseline run (e.g. jarvis-main).")
    parser.add_argument("optimized", help="Result JSON from the run being compared against it.")
    args = parser.parse_args()

    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    with open(args.optimized, encoding="utf-8") as f:
        opt = json.load(f)

    print_totals_comparison(base, opt)


if __name__ == "__main__":
    main()
