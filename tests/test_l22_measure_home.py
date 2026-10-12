"""L.22 -- the paraphrase benchmark script can produce a LIVE number.

tests/measure_l22_paraphrase.py redirects HOME to a temp dir so the rules it stores never touch
the real ~/.jarvis. Before this fix that also hid the real provider settings, so `--live` asked a
model with no providers, every answer was "no answer", and the live row silently equalled the
model-down row: a number that looked measured and was not. Pinned here (no key, no network, and
the benchmark itself is not run):

  1. prepare_home copies ONLY ai_config.json, and only for a live run; with no such file it says
     so and copies nothing (memory, conversations and channel data never travel);
  2. the real home is read BEFORE HOME is redirected, and the sandbox, copied keys included, is
     deleted when the script exits;
  3. a missing fixture (tests/fixtures/ is gitignored) is a clear message, not a traceback;
  4. the live row counts how many classifier calls were answered and says so loudly when none
     were, so an unanswered run can never be read as a result.

Run: python3 tests/test_l22_measure_home.py
"""

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tests" / "measure_l22_paraphrase.py"

# Importing the script redirects HOME to its own temp dir first (that is its job), which is also
# what keeps everything below away from the real ~/.jarvis.
_spec = importlib.util.spec_from_file_location("measure_l22_paraphrase", str(SCRIPT))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _tmp():
    return Path(tempfile.mkdtemp(prefix="jarvis-l22-measure-test-"))


def test_prepare_home_copies_only_the_provider_file():
    real, sandbox = _tmp(), _tmp()
    (real / ".jarvis" / "conversations").mkdir(parents=True)
    (real / ".jarvis" / "channels" / "people" / "x").mkdir(parents=True)
    config = {"providers": [{"name": "groq", "type": "openai_compatible", "enabled": True, "api_keys": ["sk-test"]}]}
    (real / ".jarvis" / "ai_config.json").write_text(json.dumps(config), encoding="utf-8")
    (real / ".jarvis" / "memory.json").write_text('{"facts": ["private"]}', encoding="utf-8")
    (real / ".jarvis" / "conversations" / "c1.json").write_text("{}", encoding="utf-8")
    (real / ".jarvis" / "channels" / "people" / "x" / "memory.jsonl").write_text("{}\n", encoding="utf-8")

    check("an offline run copies nothing", mod.prepare_home(real, sandbox, False) is False and not (sandbox / ".jarvis").exists())
    check("a live run reports that it copied the config", mod.prepare_home(real, sandbox, True) is True)
    copied = sorted(str(p.relative_to(sandbox)).replace("\\", "/") for p in sandbox.rglob("*") if p.is_file())
    check("and copied exactly ai_config.json, nothing else", copied == [".jarvis/ai_config.json"], copied)
    check("the copy is the file's contents", json.loads((sandbox / ".jarvis" / "ai_config.json").read_text(encoding="utf-8")) == config)
    check("the original was not touched", json.loads((real / ".jarvis" / "ai_config.json").read_text(encoding="utf-8")) == config)

    bare, sandbox2 = _tmp(), _tmp()
    check("a live run with no ai_config.json says so (False) and copies nothing",
          mod.prepare_home(bare, sandbox2, True) is False and not (sandbox2 / ".jarvis").exists())
    for d in (real, sandbox, bare, sandbox2):
        shutil.rmtree(d, ignore_errors=True)


def test_the_order_and_the_cleanup():
    src = SCRIPT.read_text(encoding="utf-8")
    real_at, redirect_at = src.index("_REAL_HOME = "), src.index('os.environ["HOME"] = _HOME')
    check("the real home is read before HOME is redirected", real_at < redirect_at, (real_at, redirect_at))
    check("--live is read from the command line before anything is imported", src.index('_LIVE = "--live" in sys.argv') < src.index("from jarvis.channels import person_memory"))
    check("prepare_home is called with that flag", "prepare_home(_REAL_HOME, _HOME, _LIVE)" in src)
    check("the sandbox (and the copied keys) is deleted at exit", "atexit.register(shutil.rmtree, _HOME, True)" in src)
    check("this test's import redirected HOME away from the real one", str(mod._HOME) != mod._REAL_HOME and mod._LIVE is False)


def test_a_missing_fixture_is_a_message():
    real_repo = mod.REPO
    empty = _tmp()
    mod.REPO = empty
    try:
        mod.load_cases()
        check("a missing fixture exits instead of returning", False)
    except SystemExit as exc:
        text = str(exc)
        check("a missing fixture exits with the path and the reason", "l22_paraphrase_cases.json" in text and ".gitignore" in text, text)
    except Exception as exc:  # noqa: BLE001
        check("a missing fixture is not a traceback", False, repr(exc))
    finally:
        mod.REPO = real_repo
        shutil.rmtree(empty, ignore_errors=True)


def test_live_row_counts_answers():
    cases = [{"topic": "salary", "directive": "don_t_discuss",
              "lexical": ["what is your salary", "tell me the salary"], "paraphrase": ["how much money do you make"],
              "negative": ["what is the weather"]}]
    pm = mod.person_memory
    real_matches, real_classify = pm.matches, pm._classify_topic

    def fake_matches(person_key, message, now=None, classify=None):
        classify("salary", message)          # every message reaches the classifier once
        return []

    pm.matches = fake_matches
    try:
        pm._classify_topic = lambda topic, message: None
        down = mod.run_mode(cases, "live")
        check("live with no provider: every call counted, none answered", down["classifier_calls"] == 4 and down["classifier_answered"] == 0, down)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.show(down)
        check("and the report says it is not a live number", "WARNING" in out.getvalue() and "no provider answered" in out.getvalue(), out.getvalue())

        pm._classify_topic = lambda topic, message: True
        up = mod.run_mode(cases, "live")
        check("live with a provider: all four answered", up["classifier_calls"] == 4 and up["classifier_answered"] == 4, up)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.show(up)
        check("and there is no warning, just the count", "WARNING" not in out.getvalue() and "answered: 4 of 4" in out.getvalue(), out.getvalue())

        pm._classify_topic = lambda topic, message: None if "weather" in message else False
        mixed = mod.run_mode(cases, "live")
        check("a partly answered run counts only the answers", mixed["classifier_answered"] == 3, mixed)

        offline = mod.run_mode(cases, "no-model")
        check("offline modes have no 'answered' figure and still count calls",
              offline["classifier_answered"] is None and offline["classifier_calls"] == 4, offline)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mod.show(offline)
        check("and print no live line", "answered" not in out.getvalue() and "WARNING" not in out.getvalue(), out.getvalue())
    finally:
        pm.matches, pm._classify_topic = real_matches, real_classify


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n== {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{name} ran without raising", False, e)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
