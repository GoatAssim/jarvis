#!/usr/bin/env python3
"""Paraphrase benchmark for L.22 standing rules (a MEASUREMENT, not a test).

    python3 tests/measure_l22_paraphrase.py              # offline modes only
    python3 tests/measure_l22_paraphrase.py --live       # also ask the real cheap model
    python3 tests/measure_l22_paraphrase.py --out FILE   # also write the numbers as JSON

Named measure_*, not test_*, so tests/run_tests.py never runs it: the numbers
are information, and a threshold set before anyone has seen them would be a
guess. The fixture (tests/fixtures/l22_paraphrase_cases.json) has, per rule
topic, messages that repeat its words ('lexical'), ask about it in other words
('paraphrase') and are unrelated ('negative').

MODES
  no-model   the classifier always says "no": what the free tiers (word overlap,
             local similarity) catch ON THEIR OWN
  model-down the classifier gives no answer: restrictive rules apply whenever the
             score is ambiguous (what an outage does)
  oracle     a perfect classifier, asked only about ambiguous scores: the best
             the current matcher can do. Paraphrases it still misses never reach
             the ambiguous band, so no model could rescue them without a change
             to the matcher itself
  live       (--live) the real cheap_call. Costs tokens.

HOME is redirected to a temp dir first; nothing real is touched.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l22-bench-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "jarvis-cli"))

from jarvis.channels import person_memory                                # noqa: E402

CLASSES = ("lexical", "paraphrase", "negative")


def load_cases():
    path = REPO / "tests" / "fixtures" / "l22_paraphrase_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def run_mode(cases, mode):
    labels = {}
    for case in cases:
        for cls in CLASSES:
            for msg in case[cls]:
                labels[msg] = cls
    calls = [0]

    def classify(topic, message):
        calls[0] += 1
        if mode == "no-model":
            return False
        if mode == "model-down":
            return None
        return labels.get(message) in ("lexical", "paraphrase")      # oracle

    rows = {cls: {"n": 0, "matched": 0, "by": {}, "missed": []} for cls in CLASSES}
    for n, case in enumerate(cases):
        person = f"discord:{7000 + n}"
        person_memory.add_instruction(person, case["topic"], case["directive"])
        for cls in CLASSES:
            for msg in case[cls]:
                got = person_memory.matches(person, msg,
                                            classify=None if mode == "live" else classify)
                row = rows[cls]
                row["n"] += 1
                if got:
                    row["matched"] += 1
                    how = got[0][1]
                    row["by"][how] = row["by"].get(how, 0) + 1
                else:
                    row["missed"].append(f"[{case['topic']}] {msg}")
    total = sum(r["n"] for r in rows.values())
    return {"mode": mode, "classifier_calls": calls[0] if mode != "live" else None,
            "messages": total, "classes": rows}


def show(result):
    print(f"\n== {result['mode']} ==")
    if result["classifier_calls"] is not None:
        print(f"classifier calls: {result['classifier_calls']} for {result['messages']} messages")
    for cls in CLASSES:
        r = result["classes"][cls]
        rate = r["matched"] / float(r["n"]) if r["n"] else 0.0
        label = "false positives" if cls == "negative" else "recall"
        print(f"{cls:<11} {r['matched']:>3}/{r['n']:<3} {label} {rate:6.1%}   via {r['by'] or '-'}")
    missed = result["classes"]["paraphrase"]["missed"]
    if missed:
        print(f"paraphrases not matched ({len(missed)}):")
        for line in missed:
            print("   ", line)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="also use the real cheap model (costs tokens)")
    ap.add_argument("--out", help="write the numbers to this JSON file")
    args = ap.parse_args()
    cases = load_cases()
    modes = ["no-model", "model-down", "oracle"] + (["live"] if args.live else [])
    results = []
    for mode in modes:
        result = run_mode(cases, mode)
        results.append(result)
        show(result)
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
