"""BUG-2 regression: a web server can never "pass" the run phase.

A server never exits, so the old run phase hit RUN_TIMEOUT, counted it as a
failure, and the fix loop rewrote working code. These tests pin the new
behaviour with REAL subprocesses (python -c), no mocks for the run itself:

  - still alive at the grace window, no crash -> ok=True, long_running=True,
    URL extracted, and the process is stopped afterwards
  - exits 0 -> ok, not long_running
  - crashes -> not ok, real stderr, not long_running
  - "timed out after Ns" with no stderr -> classified "timeout", never "unknown",
    and the fix loop is never entered for it

    python3 tests/test_dev_agent_long_running.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.actions import dev_agent  # noqa: E402
from jarvis import dev_agent_errors, dev_agent_sandbox  # noqa: E402

_tmp = []


def _dir():
    d = Path(tempfile.mkdtemp(prefix="dev_agent_lr_"))
    _tmp.append(d)
    return d


SERVER = [sys.executable, "-u", "-c",
          "import time;print('Server listening on http://localhost:3123', flush=True);time.sleep(60)"]


def test_server_still_running_is_success_and_is_stopped():
    t0 = time.monotonic()
    ok, out = dev_agent._run_long_aware(SERVER, _dir(), timeout=30, grace=1.5)
    assert ok is True, out
    assert out["long_running"] is True
    assert out["exit_code"] is None
    assert out["url"] == "http://localhost:3123", out
    assert time.monotonic() - t0 < 10, "must not wait out the 60s sleep (process must be stopped)"


def test_url_built_from_port_when_no_url_printed():
    argv = [sys.executable, "-u", "-c", "import time;print('listening on port 8080', flush=True);time.sleep(60)"]
    ok, out = dev_agent._run_long_aware(argv, _dir(), timeout=30, grace=1.5)
    assert ok and out["long_running"] and out["url"] == "http://localhost:8080", out


def test_clean_exit_is_ok_and_not_long_running():
    ok, out = dev_agent._run_long_aware([sys.executable, "-c", "print('hi')"], _dir(), timeout=30, grace=5)
    assert ok is True and out["long_running"] is False and out["exit_code"] == 0
    assert "hi" in out["stdout_tail"]


def test_crash_is_a_failure_with_real_stderr():
    argv = [sys.executable, "-c", "import sys;sys.stderr.write('boom');sys.exit(3)"]
    ok, out = dev_agent._run_long_aware(argv, _dir(), timeout=30, grace=5)
    assert ok is False and out["long_running"] is False and out["exit_code"] == 3
    assert "boom" in out["stderr_tail"]


def test_missing_executable_is_a_failure_not_a_raise():
    ok, out = dev_agent._run_long_aware(["definitely-not-a-real-binary-xyz"], _dir(), timeout=5, grace=1)
    assert ok is False and out["stderr_tail"]


def test_classifier_labels_timeout():
    assert dev_agent_errors.classify("timed out after 30s") == ("timeout", None)
    assert dev_agent_errors.is_timeout("timed out after 30s")
    # a real error that merely mentions a timeout is NOT the harness marker
    assert not dev_agent_errors.is_timeout("Error: connect ETIMEDOUT\n    at x (/a/b.js:1:2)")
    assert dev_agent_errors.classify("totally unrecognized")[0] == "unknown"


class _Ctx:
    def __init__(self):
        self.events = []

    def round_budget_remaining(self):
        return 5

    def emit_event(self, job_id, seq, phase, status, **f):
        e = {"job_id": job_id, "seq": seq, "phase": phase, "status": status, **f}
        self.events.append(e)
        return e


def _plan():
    return {"files": ["server.js"], "dependencies": [], "run_command": "node server.js",
            "files_content": {"server.js": "// x\n"}}


def test_loop_long_running_server_passes_with_zero_fix_attempts():
    run = (True, {"exit_code": None, "stdout_tail": "listening", "stderr_tail": "",
                  "long_running": True, "url": "http://localhost:3000"})
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _dir()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(_plan(), None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=run), \
         mock.patch.object(dev_agent, "_fix_files") as fix:
        r = dev_agent.tool_dev_agent({"description": "clock server"}, context=_Ctx())
    assert r["ok"] is True and r["total_attempts"] == 0
    assert r["long_running"] is True and r["url"] == "http://localhost:3000"
    fix.assert_not_called()
    assert not [s for s in r["steps"] if s["phase"] == "fix"]


def test_loop_never_fixes_a_bare_timeout():
    run = (False, {"exit_code": None, "stdout_tail": "", "stderr_tail": "timed out after 30s"})
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _dir()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(_plan(), None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=run) as rp, \
         mock.patch.object(dev_agent, "_fix_files") as fix:
        r = dev_agent.tool_dev_agent({"description": "hangs"}, context=_Ctx())
    assert r["ok"] is False and r["reason"] == "timeout"
    fix.assert_not_called()
    assert rp.call_count == 1, "no re-runs either"


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, "->", repr(e))
    for d in _tmp:
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(1 if failed else 0)
