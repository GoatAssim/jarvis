"""Regression tests for master plan Part E — the console store
(console_store.py, its wiring into ai_client.py, and the console-read/
console-clear/console-append-run CLI commands).

Covers the specific failures the master plan documents (E.2/E.3):

  1. An interrupted turn used to save an EMPTY jarvis reply with nothing
     else — abandon_pending_turn() now flushes whatever the console store
     logged before the kill and attaches it as a `consoleRef` extra.
  2. A successful ask writes one line per event (command, provider,
     narration, tool-call, tool-result, tokens, status) "as it happens",
     not derived after the fact from reply text.
  3. The console-read/console-clear/console-append-run CLI commands
     (server.js's thin-client backend for the Live Feed and the replay
     API) round-trip correctly, including the Clear marker's
     after-last-clear filtering and the `surface` split between an ask's
     own turns and a direct/live command run.
  4. Per-line and per-file capping never produces invalid JSON or a
     silent gap (E.4 point 1).

Run: python3 tests/test_console_store.py
"""

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

_JARVIS_CLI = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis import ai_client, ai_config, ai_providers, conversations, logs, console_store  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _conv_id():
    import secrets
    return secrets.token_hex(8)


@contextmanager
def _store_env():
    """Point console_store at a scratch dir, same monkeypatch-the-module-
    global convention conversations.py/logs.py's own tests already use
    (see test_interim_text.py's _ask_env)."""
    orig = console_store.CONSOLE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        console_store.CONSOLE_DIR = Path(tmp)
        try:
            yield
        finally:
            console_store.CONSOLE_DIR = orig


# ---------------------------------------------------------------------------
# Stateless layer: append / read / clear_marker / trimming
# ---------------------------------------------------------------------------

def test_append_and_read_round_trip():
    with _store_env():
        conv = _conv_id()
        console_store.append(conv, "command", "do the thing", turn="t1")
        console_store.append(conv, "provider", "anthropic", turn="t1")
        result = console_store.read(conv)
        check("two lines round-trip", len(result["lines"]) == 2, result)
        check("kinds preserved in order",
              [l["kind"] for l in result["lines"]] == ["command", "provider"], result["lines"])


def test_invalid_conv_id_is_a_silent_noop():
    with _store_env():
        seq = console_store.append("not-a-real-id", "command", "x")
        check("append refuses a bad id", seq is None, seq)
        result = console_store.read("not-a-real-id")
        check("read of a bad id returns empty, legacy", result["lines"] == [] and result["legacy"], result)


def test_seq_is_strictly_increasing_within_a_process():
    with _store_env():
        conv = _conv_id()
        seqs = [console_store.append(conv, "status", str(i)) for i in range(5)]
        check("no seq is None", all(s is not None for s in seqs), seqs)
        check("seq strictly increasing", all(seqs[i] < seqs[i + 1] for i in range(4)), seqs)


def test_since_seq_filters_correctly():
    with _store_env():
        conv = _conv_id()
        s1 = console_store.append(conv, "status", "one")
        console_store.append(conv, "status", "two")
        result = console_store.read(conv, since_seq=s1)
        check("since_seq excludes the boundary line itself, keeps later ones",
              [l["text"] for l in result["lines"]] == ["two"], result["lines"])


def test_kind_filter():
    with _store_env():
        conv = _conv_id()
        console_store.append(conv, "stdout", "a")
        console_store.append(conv, "error", "b")
        result = console_store.read(conv, kinds=["error"])
        check("kind filter keeps only the requested kind",
              [l["text"] for l in result["lines"]] == ["b"], result["lines"])


def test_turn_filter():
    with _store_env():
        conv = _conv_id()
        console_store.append(conv, "stdout", "a", turn="t1")
        console_store.append(conv, "stdout", "b", turn="t2")
        result = console_store.read(conv, turn="t1")
        check("turn filter keeps only that turn",
              [l["text"] for l in result["lines"]] == ["a"], result["lines"])


def test_surface_filter_separates_ask_from_live():
    with _store_env():
        conv = _conv_id()
        console_store.append(conv, "stdout", "from a run", surface="live")
        console_store.append(conv, "tool-call", "from an ask", surface="ask")
        live_only = console_store.read(conv, surface="live")
        ask_only = console_store.read(conv, surface="ask")
        check("surface=live excludes the ask line",
              [l["text"] for l in live_only["lines"]] == ["from a run"], live_only["lines"])
        check("surface=ask excludes the live line",
              [l["text"] for l in ask_only["lines"]] == ["from an ask"], ask_only["lines"])


def test_clear_marker_never_deletes_and_after_last_clear_filters():
    with _store_env():
        conv = _conv_id()
        console_store.append(conv, "stdout", "before clear")
        marker_seq = console_store.clear_marker(conv)
        console_store.append(conv, "stdout", "after clear")

        full = console_store.read(conv)
        check("clear_marker doesn't delete \u2014 full history still has all 3 lines",
              len(full["lines"]) == 3, full["lines"])
        check("cleared_through_seq reported", full["cleared_through_seq"] == marker_seq,
              (full["cleared_through_seq"], marker_seq))

        after = console_store.read(conv, after_last_clear=True)
        check("after_last_clear hides everything up to and including the marker",
              [l["text"] for l in after["lines"]] == ["after clear"], after["lines"])


def test_line_text_is_capped_with_a_visible_marker():
    with _store_env():
        conv = _conv_id()
        huge = "x" * (console_store.MAX_LINE_CHARS + 500)
        console_store.append(conv, "stdout", huge)
        result = console_store.read(conv)
        text = result["lines"][0]["text"]
        check("capped text is shorter than the original", len(text) < len(huge), len(text))
        check("cap leaves a visible 'chars omitted' marker", "chars omitted" in text, text)


def test_file_size_cap_trims_oldest_with_a_visible_marker():
    with _store_env():
        conv = _conv_id()
        # Force a small cap so this test doesn't need to write 2MB for real.
        orig_cap, orig_keep = console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM
        console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM = 2000, 10
        try:
            for i in range(60):
                console_store.append(conv, "stdout", f"line {i}")
            result = console_store.read(conv, limit=1000)
        finally:
            console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM = orig_cap, orig_keep
        check("trimming kept far fewer than 60 lines", len(result["lines"]) < 60, len(result["lines"]))
        check("the newest line survived trimming",
              result["lines"][-1]["text"] == "line 59", result["lines"][-1])
        check("a visible 'trimmed' marker is present",
              any("trimmed" in l["text"] for l in result["lines"] if l["kind"] == "status"),
              result["lines"])


def test_read_legacy_command_run_adapts_old_entries():
    with _store_env():
        conv = _conv_id()
        orig_jarvis_dir, orig_log_dir = logs.JARVIS_DIR, logs.LOG_DIR
        with tempfile.TemporaryDirectory() as tmp:
            logs.JARVIS_DIR, logs.LOG_DIR = Path(tmp), Path(tmp) / "logs"
            try:
                logs.log(conv, "command_run", {
                    "cmdline": "echo hi", "exit_code": 0,
                    "lines": [{"stream": "out", "text": "hi"}],
                })
                # No console_store file exists for this conv \u2014 read()
                # itself reports legacy=True; cli.py's console-read handler
                # is the one that then calls read_legacy_command_run().
                empty = console_store.read(conv)
                check("no store file yet -> legacy=True", empty["legacy"], empty)
                legacy_lines = console_store.read_legacy_command_run(conv)
                kinds = [l["kind"] for l in legacy_lines]
                check("legacy adaptation produces command+stdout+status",
                      kinds == ["command", "stdout", "status"], kinds)
                check("legacy cmdline preserved", legacy_lines[0]["text"] == "echo hi", legacy_lines[0])
                check("legacy stdout line preserved", legacy_lines[1]["text"] == "hi", legacy_lines[1])
                check("legacy exit status rendered", legacy_lines[2]["text"] == "exit 0", legacy_lines[2])
            finally:
                logs.JARVIS_DIR, logs.LOG_DIR = orig_jarvis_dir, orig_log_dir


# ---------------------------------------------------------------------------
# Active-context layer + ai_client.py wiring
# ---------------------------------------------------------------------------

def test_begin_log_end_turn_lifecycle():
    with _store_env():
        conv = _conv_id()
        turn = console_store.begin_turn(conv)
        check("begin_turn returns a turn id", bool(turn), turn)
        check("active_turn reports it", console_store.active_turn() == (conv, turn), console_store.active_turn())
        console_store.log("stdout", "hello")
        console_store.log("stdout", "world")
        pointer = console_store.end_turn("status", "answered")
        check("end_turn returns a {turn, lines} pointer",
              pointer == {"turn": turn, "lines": 3}, pointer)  # 2 logged + the status line itself
        check("active_turn is cleared after end_turn", console_store.active_turn() == (None, None),
              console_store.active_turn())
        check("log() after end_turn is a silent no-op",
              console_store.log("stdout", "too late") is None, "expected None")


def test_begin_turn_with_invalid_conv_id_makes_log_a_noop():
    with _store_env():
        turn = console_store.begin_turn("not-a-real-id")
        check("begin_turn refuses a bad id", turn is None, turn)
        check("log() is a no-op with nothing active", console_store.log("stdout", "x") is None, "expected None")


PROVIDER = {"name": "test-anthropic", "model": "claude-x", "api_key": "sk-test"}


class _FakeResp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.text = ""
        self.headers = {}

    def json(self):
        return self._data


def _queue_post_json(responses):
    it = iter(responses)

    def fake(url, headers, payload, timeout):
        return _FakeResp(next(it)), None
    return fake


@contextmanager
def _fake_http(responses):
    orig = ai_providers._post_json
    ai_providers._post_json = _queue_post_json(responses)
    try:
        yield
    finally:
        ai_providers._post_json = orig


@contextmanager
def _ask_env(providers):
    orig = (ai_config.load_ai_config, conversations.JARVIS_DIR, conversations.CONV_DIR,
            conversations.INDEX_FILE, conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR,
            console_store.CONSOLE_DIR)
    cfg = {"persona": {}, "providers": providers, "defaults": {"tools_enabled": True, "prompt_mode": "full"}}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        console_store.CONSOLE_DIR = tmp / "console"
        ai_config.load_ai_config = lambda: cfg
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, conversations.JARVIS_DIR, conversations.CONV_DIR,
             conversations.INDEX_FILE, conversations.CURRENT_FILE,
             logs.JARVIS_DIR, logs.LOG_DIR, console_store.CONSOLE_DIR) = orig


def _prov(name, keys, **extra):
    return {"name": name, "type": "anthropic", "enabled": True, "api_keys": keys, "model": "m", **extra}


def test_a_successful_ask_writes_events_as_it_happens_and_saves_a_consoleref():
    ai_providers.set_thinking("off")
    with _ask_env([_prov("anthropic", ["k1"])]) as conv:
        with _fake_http([
            {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Let me check the battery."},
                {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}},
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "It's fine."}]},
        ]):
            r = ai_client.ask("how's my battery", commands=[], conversation_id=conv)
        saved = conversations.get_conversation(conv)
        result = console_store.read(conv)
    check("ask succeeds", r.ok, r)
    kinds = [l["kind"] for l in result["lines"]]
    check("command/provider/narration/tool-call/tool-result/tokens/status all logged",
          kinds == ["command", "provider", "narration", "tool-call", "tool-result", "tokens", "status"], kinds)
    exchanges = (saved or {}).get("exchanges") or []
    extras = exchanges[0].get("extras") if exchanges else []
    refs = [e for e in (extras or []) if e.get("type") == "consoleRef"]
    check("saved exchange carries exactly one consoleRef pointer", len(refs) == 1, extras)
    if refs:
        check("consoleRef line count matches what was actually logged",
              refs[0]["data"]["lines"] == len(result["lines"]), (refs[0], len(result["lines"])))


def test_an_interrupted_turn_saves_what_ran_instead_of_nothing():
    """The master plan's headline Part E complaint: '4 of 15 exchanges have
    an empty jarvis reply' after a kill. abandon_pending_turn() must now
    flush the console store and attach a real pointer."""
    ai_providers.set_thinking("off")
    with _ask_env([_prov("anthropic", ["k1"])]) as conv:
        conversations.begin_exchange(conv, "do something risky")
        console_store.begin_turn(conv)
        console_store.log("provider", "anthropic")
        console_store.log("tool-call", "{}", tool="run_shell")
        ai_client._pending_turn[0] = (conv, "do something risky")
        ok = ai_client.abandon_pending_turn("interrupted")
        saved = conversations.get_conversation(conv)
        result = console_store.read(conv)
    check("abandon_pending_turn reports success", ok, ok)
    exchanges = (saved or {}).get("exchanges") or []
    check("exactly one exchange, marked interrupted",
          len(exchanges) == 1 and exchanges[0].get("interrupted"), exchanges)
    extras = exchanges[0].get("extras") if exchanges else []
    refs = [e for e in (extras or []) if e.get("type") == "consoleRef"]
    check("interrupted exchange still carries a consoleRef pointer (used to be nothing at all)",
          len(refs) == 1 and refs[0]["data"]["lines"] == 3, extras)
    check("the console store itself kept the 2 pre-kill lines plus the closing status",
          len(result["lines"]) == 3, result["lines"])


# ---------------------------------------------------------------------------
# CLI commands (console-read / console-clear / console-append-run) — the
# actual `python -m jarvis ...` subprocess server.js shells out to.
# ---------------------------------------------------------------------------

def _run_cli(args, env=None, stdin_text=None):
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "jarvis", *args],
        cwd=str(_JARVIS_CLI), env=full_env, input=stdin_text,
        capture_output=True, text=True, timeout=30,
    )


def test_cli_console_append_run_and_read_and_clear_round_trip():
    conv = _conv_id()
    env = {"HOME": _HOME, "USERPROFILE": _HOME, "JARVIS_CONVERSATION_ID": conv}
    payload = json.dumps({
        "turn": "live-1", "cmdline": "echo hi",
        "lines": [{"stream": "out", "text": "hi"}],
    })
    r1 = _run_cli(["console-append-run"], env=env, stdin_text=payload)
    check("console-append-run exits 0", r1.returncode == 0, r1.stderr)
    check("console-append-run reports ok", json.loads(r1.stdout).get("ok") is True, r1.stdout)

    r2 = _run_cli(["console-append-run"], env=env, stdin_text=json.dumps({"turn": "live-1", "exit_code": 0}))
    check("closing console-append-run call also ok", json.loads(r2.stdout).get("ok") is True, r2.stdout)

    r3 = _run_cli(["console-read", conv], env=env)
    read_result = json.loads(r3.stdout)
    kinds = [l["kind"] for l in read_result["lines"]]
    check("console-read sees command+stdout+status from both batches",
          kinds == ["command", "stdout", "status"], read_result)

    r4 = _run_cli(["console-clear", conv], env=env)
    clear_result = json.loads(r4.stdout)
    check("console-clear reports ok + a cleared_through_seq",
          clear_result.get("ok") is True and isinstance(clear_result.get("cleared_through_seq"), int),
          clear_result)

    r5 = _run_cli(["console-read", conv, "--after-last-clear"], env=env)
    check("console-read --after-last-clear is empty right after a clear",
          json.loads(r5.stdout)["lines"] == [], r5.stdout)


def test_cli_console_append_run_with_no_conversation_is_a_graceful_skip():
    env = {"HOME": _HOME, "USERPROFILE": _HOME}  # no JARVIS_CONVERSATION_ID
    r = _run_cli(["console-append-run"], env=env, stdin_text=json.dumps({"turn": "t", "lines": []}))
    check("no-conversation case exits 0", r.returncode == 0, r.stderr)
    check("and reports skipped rather than erroring",
          json.loads(r.stdout).get("skipped") == "no conversation", r.stdout)


def test_cli_console_read_rejects_an_invalid_conversation_id():
    r = _run_cli(["console-read", "not-a-real-id"], env={"HOME": _HOME, "USERPROFILE": _HOME})
    check("invalid id exits non-zero", r.returncode != 0, r.returncode)
    check("and reports an error", "error" in json.loads(r.stdout), r.stdout)


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    for test in _TESTS:
        test()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
