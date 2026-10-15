"""L.45 / D-3 -- the provider block's own ``agent_max_tokens``.

Owner decision 2026-10-15: the Tool Manager's Ask Jarvis agent gets its own output cap
instead of only the 8,000-token floor. This file checks the cap rule and the settings
validation; tests/test_custom_tools_agent.py checks that the cap really reaches the
provider call.

Run: python3 tests/test_l45c_agent_max_tokens.py   (no key, no network)
"""

import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l45c-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import custom_tools_agent as cta  # noqa: E402
from jarvis import settings_admin as sa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# --- the cap rule ---------------------------------------------------------------
cap = cta._output_cap
check("no fields: the 8,000 floor", cap({}) == 8000)
check("max_tokens above the floor still wins when there is no agent field", cap({"max_tokens": 32000}) == 32000)
check("max_tokens below the floor is lifted to the floor", cap({"max_tokens": 700}) == 8000)
check("agent_max_tokens is used exactly as set", cap({"agent_max_tokens": 16000}) == 16000)
check("agent_max_tokens below the floor is honoured", cap({"agent_max_tokens": 3000}) == 3000)
check("agent_max_tokens beats a bigger max_tokens", cap({"agent_max_tokens": 3000, "max_tokens": 32000}) == 3000)
check("a numeric string is accepted", cap({"agent_max_tokens": "12000"}) == 12000)
for bad in (0, -1, True, False, "lots", None, [], {}):
    check("unusable agent_max_tokens %r falls back to the old rule" % (bad,),
          cap({"agent_max_tokens": bad, "max_tokens": 20000}) == 20000)

# --- settings validation --------------------------------------------------------
def problems_for(value):
    doc = {"providers": [{"name": "p", "type": "openai_compatible", "enabled": True,
                          "agent_max_tokens": value}]}
    out = []
    sa._validate_ai_config(doc, out)
    return [p for p in out if "agent_max_tokens" in p]


check("a whole number is valid", problems_for(16000) == [])
check("1 is valid", problems_for(1) == [])
for bad in (0, -3, 1.5, "16000", True):
    got = problems_for(bad)
    check("settings refuse agent_max_tokens=%r and say why" % (bad,),
          len(got) == 1 and "providers[0].agent_max_tokens" in got[0] and "whole number" in got[0], got)

doc = {"providers": [{"name": "p", "type": "openai_compatible", "enabled": True}]}
out = []
sa._validate_ai_config(doc, out)
check("leaving the field out is fine", out == [], out)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
