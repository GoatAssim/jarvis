"""K.3.1.6 — the token line and the JARVIS_USAGE marker are emitted once per ask.

cli.py used to carry the same block twice in handle_ai_prompt, so every ask
printed the stderr "tokens:" line and the JARVIS_USAGE marker two times. It is
now one helper, _emit_usage(result), called once.

Run: python3 tests/test_k316_usage_once.py
"""

import contextlib
import io
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import cli, wire  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


SRC = (ROOT / "jarvis-cli" / "jarvis" / "cli.py").read_text(encoding="utf-8")

# --- source shape ------------------------------------------------------------
check("cli.py prints the JARVIS_USAGE marker from exactly one place",
      len(re.findall(r'print\(\s*"JARVIS_USAGE "', SRC)) == 1,
      str(len(re.findall(r'print\(\s*"JARVIS_USAGE "', SRC))))
check("handle_ai_prompt calls _emit_usage exactly once",
      len(re.findall(r"^\s+_emit_usage\(result\)", SRC, re.M)) == 1)

# --- behaviour ---------------------------------------------------------------
traces = []
_orig_trace = wire.trace
wire.trace = lambda text, kind=None, *a, **k: traces.append((text, kind))
try:
    usage = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
             "rounds": [1, 2], "tool_calls": ["a"],
             "ask_total": {"total_tokens": 10, "limit": 150000}}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._emit_usage(SimpleNamespace(usage=usage))
    lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("JARVIS_USAGE ")]
    check("one JARVIS_USAGE line per call", len(lines) == 1, str(lines))
    check("the marker carries the usage json",
          bool(lines) and json.loads(lines[0][len("JARVIS_USAGE "):]).get("total_tokens") == 10)
    token_lines = [t for t, k in traces if "tokens: in=7 out=3 total=10" in t]
    check("one human 'tokens:' trace line per call", len(token_lines) == 1, str(traces))
    check("rounds and tool counts are in that line",
          bool(token_lines) and "rounds=2 tools=1" in token_lines[0])

    traces.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._emit_usage(SimpleNamespace(usage=None))
        cli._emit_usage(SimpleNamespace(usage={}))
    check("no usage means no output at all", buf.getvalue() == "" and not traces)
finally:
    wire.trace = _orig_trace

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
