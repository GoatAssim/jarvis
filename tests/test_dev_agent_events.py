"""Conformance test for dev_agent_events.py against the §3.6 plan's §3.1
(envelope) and §3.2 (event shape) spec:

- §3.1: every emitted line is `JARVIS_MEDIA\\tdev_agent\\t<json>`, exactly
  3 tab-separated parts — the same `line.split("\\t")` pattern
  addAskPromptTrace already uses for present_file/screenshot/ytdl events.
- §3.2: every event dict carries at least job_id/seq/phase/status, plus
  whatever per-phase fields the caller (dev_agent.py) passes through.

No test framework dependency — plain asserts, runnable directly:

    python3 tests/test_dev_agent_events.py
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import dev_agent_events  # noqa: E402


class _CaptureStderr:
    """Swap sys.stderr for a StringIO for the duration of the block, and
    make sure dev_agent_events.set_hook(None) afterward so one test's hook
    can never leak into the next (module-level global)."""

    def __enter__(self):
        self._orig = sys.stderr
        sys.stderr = io.StringIO()
        return sys.stderr

    def __exit__(self, *exc):
        sys.stderr = self._orig
        dev_agent_events.set_hook(None)


def test_envelope_is_exactly_three_tab_fields():
    with _CaptureStderr() as buf:
        dev_agent_events.emit("da_test_1", 0, "plan", "start", description="build a thing")
    line = buf.getvalue().rstrip("\n")
    parts = line.split("\t")
    assert len(parts) == 3, f"expected exactly 3 tab-separated parts, got {len(parts)}: {parts!r}"
    assert parts[0] == "JARVIS_MEDIA"
    assert parts[1] == "dev_agent"
    json.loads(parts[2])  # must be valid JSON — raises if not


def test_event_shape_has_required_base_fields_plus_extras():
    with _CaptureStderr() as buf:
        returned = dev_agent_events.emit(
            "da_test_2", 3, "write", "ok", file="app.py", bytes_written=120, preview="print(1)"
        )
    line = buf.getvalue().rstrip("\n")
    payload = json.loads(line.split("\t", 2)[2])
    for event in (payload, returned):
        assert event["job_id"] == "da_test_2"
        assert event["seq"] == 3
        assert event["phase"] == "write"
        assert event["status"] == "ok"
        assert event["file"] == "app.py"
        assert event["bytes_written"] == 120
        assert event["preview"] == "print(1)"
    # emit() must hand the caller back the exact same dict that was
    # serialized, so a `steps` list built from return values can never
    # drift from what was actually streamed.
    assert payload == returned


def test_fields_with_embedded_tabs_and_newlines_dont_break_the_split():
    # A run's stdout/stderr tail can contain real tabs and newlines —
    # json.dumps must escape them so the emitted line still splits into
    # exactly 3 parts (this is the guarantee §3.1 leans on to reuse
    # addAskPromptTrace's plain line.split("\t") dispatch unmodified).
    with _CaptureStderr() as buf:
        dev_agent_events.emit(
            "da_test_3", 0, "run", "fail",
            command="python app.py",
            stdout_tail="line one\nline two\twith a tab",
        )
    line = buf.getvalue().rstrip("\n")
    parts = line.split("\t")
    assert len(parts) == 3
    payload = json.loads(parts[2])
    assert payload["stdout_tail"] == "line one\nline two\twith a tab"


def test_unserializable_field_falls_back_without_raising():
    class Unserializable:
        def __repr__(self):
            return "<Unserializable>"

    with _CaptureStderr() as buf:
        returned = dev_agent_events.emit(
            "da_test_4", 1, "fix", "fail", target_file=Unserializable()
        )
    line = buf.getvalue().rstrip("\n")
    parts = line.split("\t")
    assert len(parts) == 3
    payload = json.loads(parts[2])
    # Fallback event still carries the required base fields and an
    # explanatory error, and never raises out of emit() itself.
    assert payload["job_id"] == "da_test_4"
    assert payload["seq"] == 1
    assert payload["phase"] == "fix"
    assert payload["status"] == "fail"
    assert "error" in payload
    assert returned == payload


def test_seq_is_whatever_the_caller_passes_through_unchanged():
    # dev_agent_events.py doesn't own sequencing — §3.3 says dev_agent.py
    # keeps its own local counter and passes it in — emit() just has to
    # carry it through faithfully, including across out-of-order calls
    # (the plan calls this "cheap insurance", not a guarantee emit()
    # itself enforces ordering).
    seqs = []
    with _CaptureStderr() as buf:
        for seq in (0, 1, 2):
            seqs.append(dev_agent_events.emit("da_test_5", seq, "install", "progress")["seq"])
    assert seqs == [0, 1, 2]


def test_set_hook_receives_the_same_event_dict():
    seen = []
    dev_agent_events.set_hook(seen.append)
    with _CaptureStderr():
        returned = dev_agent_events.emit("da_test_6", 0, "done", "ok", project_dir="/tmp/x")
    assert len(seen) == 1
    assert seen[0] == returned


def test_hook_exception_never_propagates_out_of_emit():
    def bad_hook(event):
        raise RuntimeError("boom")

    dev_agent_events.set_hook(bad_hook)
    with _CaptureStderr() as buf:
        returned = dev_agent_events.emit("da_test_7", 0, "error", "fail", error="boom")
    # emit() must still have printed the line and returned the event —
    # a broken hook can't take the agent loop down over a logging call.
    line = buf.getvalue().rstrip("\n")
    assert line.split("\t")[1] == "dev_agent"
    assert returned["job_id"] == "da_test_7"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
