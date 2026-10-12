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

HOME is redirected to a temp dir first, so the rules this script stores never touch your real
~/.jarvis. With --live ONLY ~/.jarvis/ai_config.json (the providers and their keys) is copied
into that temp dir, because the real model call finds its providers there; without the copy
every live answer would be "no answer" and the live row would silently equal model-down. The
temp dir, copy included, is deleted when the script exits. Nothing is ever written back.

The fixture lives in tests/fixtures/, which .gitignore excludes: a fresh clone or a zip made
from git does not have it, and the script says so instead of failing with a traceback.
"""

import argparse
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def prepare_home(real_home, temp_home, live):
    """Copy the one file a live run needs into the sandbox HOME. Returns True when it did.
    Only ai_config.json, and only for --live: no memory, conversations or channel data."""
    if not live:
        return False
    source = Path(real_home) / ".jarvis" / "ai_config.json"
    if not source.is_file():
        return False
    target = Path(temp_home) / ".jarvis"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source), str(target / "ai_config.json"))
    return True


_LIVE = "--live" in sys.argv[1:]
_REAL_HOME = os.path.expanduser("~")          # read BEFORE HOME is redirected below
_HOME = tempfile.mkdtemp(prefix="jarvis-l22-bench-")
_CONFIG_COPIED = prepare_home(_REAL_HOME, _HOME, _LIVE)
atexit.register(shutil.rmtree, _HOME, True)   # the copied keys must not outlive the run
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "jarvis-cli"))

from jarvis.channels import person_memory                                # noqa: E402

CLASSES = ("lexical", "paraphrase", "negative")


def load_cases():
    path = REPO / "tests" / "fixtures" / "l22_paraphrase_cases.json"
    if not path.is_file():
        sys.exit(f"The fixture is not in this checkout: {path}\n"
                 "tests/fixtures/ is in .gitignore, so a fresh clone or a zip made from git does not have it. "
                 "Restore l22_paraphrase_cases.json (it came with the L.22 patch, "
                 "jarvis-l21-l22-export-postcheck-conflicts.patch) and run again.")
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def run_mode(cases, mode):
    labels = {}
    for case in cases:
        for cls in CLASSES:
            for msg in case[cls]:
                labels[msg] = cls
    calls = [0]
    answered = [0]

    def classify(topic, message):
        calls[0] += 1
        if mode == "no-model":
            return False
        if mode == "model-down":
            return None
        if mode == "live":
            verdict = person_memory._classify_topic(topic, message)   # the real cheap call
            if verdict is not None:
                answered[0] += 1
            return verdict
        return labels.get(message) in ("lexical", "paraphrase")      # oracle

    rows = {cls: {"n": 0, "matched": 0, "by": {}, "missed": []} for cls in CLASSES}
    for n, case in enumerate(cases):
        person = f"discord:{7000 + n}"
        person_memory.add_instruction(person, case["topic"], case["directive"])
        for cls in CLASSES:
            for msg in case[cls]:
                got = person_memory.matches(person, msg, classify=classify)
                row = rows[cls]
                row["n"] += 1
                if got:
                    row["matched"] += 1
                    how = got[0][1]
                    row["by"][how] = row["by"].get(how, 0) + 1
                else:
                    row["missed"].append(f"[{case['topic']}] {msg}")
    total = sum(r["n"] for r in rows.values())
    return {"mode": mode, "classifier_calls": calls[0],
            "classifier_answered": answered[0] if mode == "live" else None,
            "messages": total, "classes": rows}


def show(result):
    print(f"\n== {result['mode']} ==")
    print(f"classifier calls: {result['classifier_calls']} for {result['messages']} messages")
    if result["classifier_answered"] is not None:
        print(f"classifier answered: {result['classifier_answered']} of {result['classifier_calls']}")
        if result["classifier_calls"] and not result["classifier_answered"]:
            print("WARNING: no provider answered, so this row is the same as model-down and is not a "
                  "live number. Check that ~/.jarvis/ai_config.json has an enabled provider with a key.")
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
    if args.live and not _CONFIG_COPIED:
        print("NOTE: no ~/.jarvis/ai_config.json was found, so the live model has no provider to ask.")
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
