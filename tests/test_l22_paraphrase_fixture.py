"""Checks on the L.22 paraphrase fixture, and the guarantees the matcher gives
for it without a model.

    python3 tests/test_l22_paraphrase_fixture.py

This test asserts only what the code GUARANTEES: that the fixture is what it
says it is, and that a message with at least half of a rule's topic words
matches lexically with no model call. How well the matcher does on the
PARAPHRASES is not asserted here: that number is a measurement, not a
requirement, and it is printed by tests/measure_l22_paraphrase.py.

HOME is redirected to a temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import json
import os
import py_compile
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l22-fixture-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "jarvis-cli"))

from jarvis.channels import person_memory                                # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


cases = json.loads((REPO / "tests" / "fixtures" / "l22_paraphrase_cases.json").read_text(encoding="utf-8"))["cases"]
check("the fixture has at least 10 topics", len(cases) >= 10, str(len(cases)))
check("topics are all different", len({c["topic"] for c in cases}) == len(cases))

calls = []


def never(topic, message):
    calls.append((topic, message))
    return False


for n, case in enumerate(cases):
    topic = case["topic"]
    keywords = set(person_memory.derive_keywords(topic))
    check(f"[{topic}] the topic has usable keywords", len(keywords) >= 2, str(keywords))
    check(f"[{topic}] the directive is one a topic can have",
          case["directive"] in ("don_t_discuss", "ask_owner_first", "notify_owner"))
    check(f"[{topic}] it has lexical, paraphrase and negative messages",
          len(case["lexical"]) >= 2 and len(case["paraphrase"]) >= 3 and len(case["negative"]) >= 2)

    for msg in case["lexical"]:
        share = len(person_memory._stems(msg) & keywords) / float(len(keywords))
        check(f"[{topic}] lexical '{msg}' has at least half the keywords", share >= person_memory.STRONG_OVERLAP,
              f"{share:.2f}")
    for msg in case["paraphrase"]:
        shared = person_memory._stems(msg) & keywords
        check(f"[{topic}] paraphrase '{msg}' shares no keyword", not shared, str(shared))
    for msg in case["negative"]:
        shared = person_memory._stems(msg) & keywords
        check(f"[{topic}] negative '{msg}' shares no keyword", not shared, str(shared))

    person = f"discord:{9000 + n}"
    rec, err = person_memory.add_instruction(person, topic, case["directive"])
    check(f"[{topic}] the rule is created", rec and not err, err)
    for msg in case["lexical"]:
        del calls[:]
        got = person_memory.matches(person, msg, classify=never)
        check(f"[{topic}] lexical '{msg}' matches", len(got) == 1 and got[0][1] == "lexical", str(got))
        check(f"[{topic}] ...with no model call", not calls, str(calls))

# no case reuses another case's messages, so one rule cannot be "answered" by
# another topic's lexical examples
every = [m for c in cases for m in c["lexical"] + c["paraphrase"] + c["negative"]]
check("no message appears twice", len(every) == len(set(every)))

bench = REPO / "tests" / "measure_l22_paraphrase.py"
check("the measurement script exists", bench.exists())
try:
    py_compile.compile(str(bench), doraise=True)
    compiled = True
except py_compile.PyCompileError as exc:
    compiled = False
    print(exc)
check("the measurement script compiles", compiled)
check("the measurement script is not named test_* (it must not run with the suite)",
      not bench.name.startswith("test_"))

print(f"{PASSED} passed, {len(FAILED)} failed")
for line in FAILED:
    print("FAILED:", line)
sys.exit(1 if FAILED else 0)
