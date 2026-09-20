"""F.12 log mislabelling: the AI risk review makes a request to a DIFFERENT
provider while an attempt is running; its request/response used to be logged
under the running attempt's label (`gemini (key 2/10)` next to an
api.groq.com URL), so the Logs viewer credited the wrong key. Run:
python3 tests/test_log_labels.py"""
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers as ap, logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class Resp:
    status_code, text = 200, "{}"

    def json(self):
        return {}


def _labels(url):
    seen = []
    orig_log, orig_post = logs.log, ap.requests.post
    logs.log = lambda conv, kind, data, provider=None: seen.append((kind, provider))
    ap.requests.post = lambda *a, **k: Resp()
    try:
        ap._post_json(url, {}, {}, 5)
    finally:
        logs.log, ap.requests.post = orig_log, orig_post
    return seen


def test_labels():
    ap.set_log_context("c1", "gemini (key 2/10)", base_url="https://generativelanguage.googleapis.com/v1beta/models/x")
    own = _labels("https://generativelanguage.googleapis.com/v1beta/models/x:generate")
    side = _labels("https://api.groq.com/openai/v1/chat/completions")
    ap.clear_log_context()
    check("the attempt's own request keeps its key label", own and all(p == "gemini (key 2/10)" for _k, p in own), own)
    check("a request to another host is not credited to that key", side and all("api.groq.com" in p and p != "gemini (key 2/10)" for _k, p in side), side)
    check("the side label still says which attempt it happened during", all("gemini (key 2/10)" in p for _k, p in side), side)
    ap.set_log_context("c1", "ollama")   # no base_url known -> never relabel
    check("no known host -> unchanged behaviour", all(p == "ollama" for _k, p in _labels("http://localhost:11434/api/chat")))
    ap.clear_log_context()


test_labels()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
