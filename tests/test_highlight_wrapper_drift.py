"""F.10 follow-up: catch drift between web/server.js and ai_client.py.

`web/server.js` wraps a highlighted excerpt in a fixed sentence before it
reaches Jarvis; `ai_client._strip_highlight_excerpt` mirrors that wording so
the excerpt (text the user is pointing AT) doesn't vote in the router. The two
are separate languages with no shared source, so if someone rewords the JS the
Python strip silently stops matching and the original routing bug returns.
tests/test_router_last_line_and_highlight.py can only exercise the Python side.

This test reads the wrapper's string literals straight out of server.js,
rebuilds the exact prompt the JS would send, and feeds it to the Python strip.

If it fails: change `_HIGHLIGHT_WRAPPER` / `_HIGHLIGHT_DEFAULT_ASK` in
ai_client.py to match server.js (or the reverse), in the same change.
Run: python3 tests/test_highlight_wrapper_drift.py"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "jarvis-cli"))

from jarvis import ai_client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _wrapper_literals():
    """The string literals server.js concatenates around `quote`/`text`, in
    order, decoded. None if the block can't be found."""
    src = (_ROOT / "web" / "server.js").read_text(encoding="utf-8")
    m = re.search(r"let prompt = text;(.*?)if \(prompt\.length", src, re.S)
    if not m:
        return None
    lits = []
    for dq, sq in re.findall(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'', m.group(1)):
        if sq or not dq:
            # single-quoted JS literal -> decode via a double-quoted JSON string
            body = sq.replace("\\'", "'").replace('"', '\\"')
            lits.append(json.loads('"' + body + '"'))
        else:
            lits.append(json.loads('"' + dq + '"'))
    return lits


def _build(lits, quote, text):
    # Mirrors: header + '"""\n' + quote + "\n" + '"""\n\n' + (text || default)
    return lits[0] + lits[1] + quote + lits[2] + lits[3] + (text or lits[4])


def test_wrapper_matches():
    lits = _wrapper_literals()
    check("server.js's highlight wrapper block was found", lits is not None)
    if lits is None:
        return
    check("it still has the five literals this test (and the Python mirror) expect",
          len(lits) == 5, lits)
    if len(lits) != 5:
        return
    check("server.js's default ask equals ai_client._HIGHLIGHT_DEFAULT_ASK",
          lits[4] == ai_client._HIGHLIGHT_DEFAULT_ASK, lits[4])

    quote = "Let me know if you need more.\nThen I can schedule it."
    own = "run this custom command"
    got = ai_client._strip_highlight_excerpt(_build(lits, quote, own))
    check("a prompt built exactly as server.js builds it strips to the user's own words", got == own, got)
    got = ai_client._strip_highlight_excerpt(_build(lits, quote, ""))
    check("with nothing typed, the stock default ask routes as empty", got == "", got)
    got = ai_client._strip_highlight_excerpt(_build(lits, "one line", "hi"))
    check("a single-line quote strips too", got == "hi", got)


test_wrapper_matches()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
