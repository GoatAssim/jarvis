"""Master plan K.3.2.4: the word-by-word tier of the subagent live view -- the
Python half (task_stream.py, task_runner._spawn_ask's hook, ask_output's
protocol-line stripping).

What this pins down:
  * a task id only becomes a stream-file path if tasks.py would accept it;
  * begin_step/end_step write `JARVIS_STREAM {json}` lines (the same shape
    stream_markers.py writes) and never raise, even with the escape hatch on or
    a bad task;
  * a file over MAX_BYTES restarts at the next step_start, and old files are
    pruned;
  * _spawn_ask hands the child JARVIS_TASK_STREAM_FILE for a valid task id (and
    ONLY then), drops a stale inherited one, honours JARVIS_SUBAGENT_STREAM=0,
    and brackets the step with step_start / step_end in every exit path;
  * StreamMarkerSink(out=<that file>) produces events in the order the web
    endpoint (GET /api/subagents/:id/stream) and app.js expect;
  * JARVIS_STREAM lines are stripped from captured replies like the other
    protocol lines.

NOT covered here (needs a running server / browser, see the master plan K.3.2.4):
the /api/subagents/:id/stream route in web/server.js, and the pane in app.js.

Run: python3 tests/test_task_stream.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Redirect HOME BEFORE importing any jarvis module (AGENTS.md > Testing).
_HOME = tempfile.mkdtemp(prefix="jarvis-task-stream-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import ask_output, task_runner, task_stream  # noqa: E402
from jarvis.stream_markers import MARKER, StreamMarkerSink  # noqa: E402

GOOD_ID = "t_0a1b2c3d"


def _lines(path):
    return [json.loads(l[len(MARKER):]) for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.startswith(MARKER)]


def _fresh(task_id=GOOD_ID):
    p = task_stream.path_for(task_id)
    if p is not None and p.exists():
        p.unlink()
    return p


class _Env:
    """Set/unset env vars for one test, restoring them after."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- task_stream ---------------------------------------------------------------

def test_a_task_id_only_becomes_a_path_when_tasks_py_would_accept_it():
    assert task_stream.path_for(GOOD_ID) == task_stream.stream_dir() / (GOOD_ID + ".jsonl")
    for bad in ("t1", "task-1", "", None, "../t_0a1b2c3d", "t_0a1b2c3d/../../x", "t_ZZZZZZZZ"):
        assert task_stream.path_for(bad) is None, bad


def test_begin_step_writes_a_step_start_line_in_the_marker_format():
    _fresh()
    with _Env(JARVIS_SUBAGENT_STREAM=None):
        path = task_stream.begin_step({"id": GOOD_ID}, "plan")
    assert path and Path(path).exists()
    raw = Path(path).read_text(encoding="utf-8").splitlines()
    assert len(raw) == 1 and raw[0].startswith(MARKER), raw
    ev = json.loads(raw[0][len(MARKER):])
    assert ev["k"] == "step_start" and ev["step"] == "plan" and ev["ts"], ev


def test_steps_append_to_one_file_and_end_step_records_the_outcome():
    _fresh()
    with _Env(JARVIS_SUBAGENT_STREAM=None):
        p1 = task_stream.begin_step({"id": GOOD_ID}, "plan")
        task_stream.end_step(p1, True)
        p2 = task_stream.begin_step({"id": GOOD_ID}, 1)
        task_stream.end_step(p2, False)
    assert p1 == p2
    kinds = [(e["k"], e.get("step", e.get("ok"))) for e in _lines(p1)]
    assert kinds == [("step_start", "plan"), ("step_end", True), ("step_start", 1), ("step_end", False)], kinds


def test_the_escape_hatch_and_bad_input_never_raise_and_write_nothing():
    _fresh()
    with _Env(JARVIS_SUBAGENT_STREAM="0"):
        assert task_stream.begin_step({"id": GOOD_ID}, 1) == ""
    assert not task_stream.path_for(GOOD_ID).exists()
    with _Env(JARVIS_SUBAGENT_STREAM=None):
        assert task_stream.begin_step({"id": "t1"}, 1) == ""
        assert task_stream.begin_step(None, 1) == ""
        assert task_stream.begin_step({}, 1) == ""
    task_stream.end_step("", True)                              # empty path: a no-op
    task_stream.end_step("/nonexistent-dir/x/y.jsonl", True)    # unwritable: swallowed


def test_a_file_over_the_cap_restarts_at_the_next_step_start():
    p = _fresh()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(("x" * 1000 + "\n") * (task_stream.MAX_BYTES // 1000 + 5), encoding="utf-8")
    assert p.stat().st_size > task_stream.MAX_BYTES
    with _Env(JARVIS_SUBAGENT_STREAM=None):
        task_stream.begin_step({"id": GOOD_ID}, 2)
    evs = _lines(p)
    assert [e["k"] for e in evs] == ["step_start"] and p.stat().st_size < 1000, (len(evs), p.stat().st_size)


def test_old_stream_files_are_pruned_and_fresh_ones_kept():
    old_id, new_id = "t_00000001", "t_00000002"
    po, pn = task_stream.path_for(old_id), task_stream.path_for(new_id)
    po.parent.mkdir(parents=True, exist_ok=True)
    po.write_text("", encoding="utf-8")
    pn.write_text("", encoding="utf-8")
    ancient = time.time() - task_stream.KEEP_SECONDS - 3600
    os.utime(po, (ancient, ancient))
    with _Env(JARVIS_SUBAGENT_STREAM=None):
        task_stream.begin_step({"id": GOOD_ID}, 1)
    assert not po.exists() and pn.exists()


# --- task_runner._spawn_ask ------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout="ok reply", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _spawn_with_fake_run(task, fake):
    """Run _spawn_ask with subprocess.run replaced by `fake(argv, **kw)`; return
    (result tuple, the env the child would have received)."""
    seen = {}
    orig = task_runner.subprocess.run

    def run(argv, **kw):
        seen["env"] = kw.get("env") or {}
        return fake(argv, **kw)

    task_runner.subprocess.run = run
    try:
        out = task_runner._spawn_ask(task, "do the thing")
    finally:
        task_runner.subprocess.run = orig
    return out, seen.get("env", {})


def test_spawn_ask_gives_a_valid_task_its_stream_file_and_brackets_the_step():
    _fresh()
    with _Env(JARVIS_SUBAGENT_STREAM=None, JARVIS_TASK_STREAM_FILE=None):
        (ok, text, err), env = _spawn_with_fake_run({"id": GOOD_ID, "cursor": 1, "plan": ["a", "b"]},
                                                    lambda argv, **kw: _FakeProc())
    assert ok and text == "ok reply" and err is None
    assert env.get("JARVIS_TASK_STREAM_FILE") == str(task_stream.path_for(GOOD_ID)), env.get("JARVIS_TASK_STREAM_FILE")
    evs = _lines(task_stream.path_for(GOOD_ID))
    assert [e["k"] for e in evs] == ["step_start", "step_end"], evs
    assert evs[0]["step"] == 2 and evs[1]["ok"] is True, evs      # cursor 1 -> the 2nd step


def test_spawn_ask_labels_the_planning_step_plan():
    _fresh()
    with _Env(JARVIS_SUBAGENT_STREAM=None):
        _spawn_with_fake_run({"id": GOOD_ID}, lambda argv, **kw: _FakeProc())
    assert _lines(task_stream.path_for(GOOD_ID))[0]["step"] == "plan"


def test_spawn_ask_closes_the_step_as_failed_on_every_failure_exit():
    import subprocess

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    def oserror(argv, **kw):
        raise OSError("nope")

    cases = {
        "timeout": (timeout, "timed out"),
        "oserror": (oserror, "couldn't run"),
        "nonzero, no output": (lambda argv, **kw: _FakeProc(stdout="", stderr="boom", returncode=1), "boom"),
    }
    for label, (fake, needle) in cases.items():
        _fresh()
        with _Env(JARVIS_SUBAGENT_STREAM=None):
            (ok, text, err), _env = _spawn_with_fake_run({"id": GOOD_ID, "plan": ["a"]}, fake)
        assert not ok and needle in (err or ""), (label, err)
        evs = _lines(task_stream.path_for(GOOD_ID))
        assert [e["k"] for e in evs] == ["step_start", "step_end"] and evs[1]["ok"] is False, (label, evs)


def test_spawn_ask_leaves_the_child_alone_for_a_bad_id_the_escape_hatch_or_a_stale_var():
    with _Env(JARVIS_SUBAGENT_STREAM=None, JARVIS_TASK_STREAM_FILE="/stale/from/outer/run"):
        _, env = _spawn_with_fake_run({"id": "t1"}, lambda argv, **kw: _FakeProc())
        assert "JARVIS_TASK_STREAM_FILE" not in env, "an inherited stream file must never ride along"
    _fresh()
    with _Env(JARVIS_SUBAGENT_STREAM="0", JARVIS_TASK_STREAM_FILE=None):
        _, env = _spawn_with_fake_run({"id": GOOD_ID}, lambda argv, **kw: _FakeProc())
    assert "JARVIS_TASK_STREAM_FILE" not in env
    assert not task_stream.path_for(GOOD_ID).exists()


# --- the wire: StreamMarkerSink writing into the stream file ---------------------

def test_stream_marker_sink_into_the_task_file_yields_the_events_the_endpoint_expects():
    p = _fresh()
    with _Env(JARVIS_SUBAGENT_STREAM=None):
        path = task_stream.begin_step({"id": GOOD_ID}, 1)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:      # what cli.py does
        sink = StreamMarkerSink(out=fh, coalesce_seconds=0.0)
        sink("thinking", delta="hm ")
        sink("text", delta="Looking ")
        sink("text", delta="now.")
        sink("tool", name="search_files")
        sink("round_end", finish="tool")
        sink("text", delta="Done.")
        sink("round_end", finish="done")
        sink.close()
    task_stream.end_step(path, True)
    kinds = [e["k"] for e in _lines(p)]
    assert kinds[0] == "step_start" and kinds[-1] == "step_end", kinds
    assert {"thinking", "text", "tool", "round_end"} <= set(kinds), kinds
    assert kinds.index("tool") < kinds.index("round_end"), kinds
    # every line is a whole JSON event: nothing a line-oriented reader would tear
    assert all(l.startswith(MARKER) for l in p.read_text(encoding="utf-8").splitlines())


# --- ask_output ---------------------------------------------------------------------

def test_stream_marker_lines_are_stripped_from_a_captured_reply():
    raw = "\n".join([MARKER + '{"k":"text","d":"hi"}', "the real reply", MARKER + '{"k":"round_end","finish":"done"}'])
    assert ask_output.strip_protocol_lines(raw) == "the real reply"
    assert ask_output.strip_protocol_lines("JARVIS_USAGE {}\nkept") == "kept", "existing prefixes still stripped"


# --- runner: keep test functions ABOVE this block (AGENTS.md > Testing) ---
if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in fns:
        try:
            f()
            print(f"ok   {n}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {n}\n{e}")
        except Exception as e:  # noqa: BLE001 -- surface a crash as a failure, not a traceback wall
            failed += 1
            print(f"FAIL {n}\n{type(e).__name__}: {e}")
    print(f"{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
