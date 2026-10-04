"""Master plan L.38 -- the raw event log (jarvis/raw_archive.py).

The promise: the raw events of a conversation are stored as they happen --
uncapped, unsplit, unsummarised -- so a view can be rebuilt from them later
without the storage changing.

  1. A real ask() (fake HTTP provider, real tool executor) writes user,
     console, tool_run, model_reply and reply events.
  2. The model's reply is stored BEFORE the console-dump split, thinking is
     stored in FULL (the saved extra is clipped), a tool result is stored at
     full size.
  3. rebuild_exchanges() reproduces the live conversation file's exchanges,
     including past the 60-cap, a redo, an interrupted turn and a turn that
     never resolved -- the proof the log is sufficient to rebuild the view.
  4. console_store.read(full=True) restores a clipped line from the log; the
     default read and a conversation with no log behave exactly as before.
  5. rebuild_console() returns lines the console store's size cap rotated out.
  6. Clear / delete remove the log; JARVIS_RAW_ARCHIVE=0 writes nothing; a bad
     id, an unwritable dir and a torn last line never raise.
  7. A real `conv-export --raw` and `console-read` (--full) process.
  8. The stores the UI reads did not change shape or caps.

Run: python3 tests/test_l38_raw_archive.py   (no key, no network)
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l38-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("JARVIS_RAW_ARCHIVE", None)

_JARVIS_CLI = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis import (ai_client, ai_config, ai_providers, console_store,  # noqa: E402
                    conv_export, conversations, logs, raw_archive)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # cp1252 consoles (Windows) choke on test details
    except Exception:  # noqa: BLE001
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _conv_id():
    return secrets.token_hex(8)


@contextmanager
def _env(var, value):
    old = os.environ.get(var)
    os.environ[var] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old


@contextmanager
def _dirs():
    """Scratch dirs for every store, same monkeypatch convention as
    test_console_store.py."""
    orig = (conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
            conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR,
            console_store.CONSOLE_DIR, raw_archive.ARCHIVE_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        console_store.CONSOLE_DIR = tmp / "console"
        raw_archive.ARCHIVE_DIR = tmp / "events"
        try:
            yield tmp
        finally:
            (conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
             conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR,
             console_store.CONSOLE_DIR, raw_archive.ARCHIVE_DIR) = orig


class _FakeResp:
    def __init__(self, data):
        self.status_code, self._data, self.text, self.headers = 200, data, "", {}

    def json(self):
        return self._data


@contextmanager
def _fake_http(responses):
    it = iter(responses)
    orig = ai_providers._post_json
    ai_providers._post_json = lambda url, headers, payload, timeout: (_FakeResp(next(it)), None)
    try:
        yield
    finally:
        ai_providers._post_json = orig


def _prov():
    return {"name": "anthropic", "type": "anthropic", "enabled": True,
            "api_keys": ["k1"], "model": "m"}


@contextmanager
def _ask_env():
    orig = ai_config.load_ai_config
    cfg = {"persona": {}, "providers": [_prov()],
           "defaults": {"tools_enabled": True, "prompt_mode": "full"}}
    with _dirs():
        ai_config.load_ai_config = lambda: cfg
        try:
            yield conversations.get_current_id()
        finally:
            ai_config.load_ai_config = orig


def _seed(conv, n):
    for i in range(n):
        conversations.append_exchange(conv, f"q{i} \u00e9\u4e2d\U0001F600 " + "x" * 50, f"a{i}", "test")


def _norm(exs):
    """The fields a view is built from; ts differs by a few ms between stores."""
    return [(e.get("user"), e.get("jarvis"), e.get("provider"),
             bool(e.get("interrupted")), bool(e.get("pending"))) for e in exs]


# ---- 1 + 2. a real ask() ----------------------------------------------------

def test_real_ask_writes_raw_events():
    ai_providers.set_thinking("off")
    typed = "  how's my battery?  \n(second line, odd   spacing)  "
    reply = "It's fine.\n$ battery 91%\nThat is all."
    with _ask_env() as conv:
        with _fake_http([
            {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Let me check the battery."},
                {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": reply}]},
        ]):
            r = ai_client.ask(typed, commands=[], conversation_id=conv)
        events = raw_archive.read(conv)
        kinds = [e["kind"] for e in events]
        saved = conversations.get_conversation(conv)["exchanges"]
        cons = raw_archive.rebuild_console(conv)
        store_seqs = [l["seq"] for l in console_store.read(conv)["lines"]]
        rebuilt = raw_archive.rebuild_exchanges(conv)
    check("ask succeeds", r.ok, r)
    check("event kinds include user, console, tool_run, model_reply, reply",
          all(k in kinds for k in ("user", "console", "tool_run", "model_reply", "reply")), kinds)
    users = [e for e in events if e["kind"] == "user"]
    check("the user's input is stored byte for byte (spacing, newlines)",
          len(users) == 1 and users[0]["text"] == typed, users)
    mr = [e for e in events if e["kind"] == "model_reply"]
    check("exactly one model_reply", len(mr) == 1, kinds)
    if mr:
        check("model_reply keeps the model's text unsplit", mr[0]["text"] == reply, mr[0]["text"])
        check("model_reply records provider and the interim narration",
              mr[0]["provider"] and "Let me check the battery." in json.dumps(mr[0]["interim"]), mr[0])
        check("model_reply carries the turn trace", isinstance(mr[0].get("trace"), dict))
    tr = [e for e in events if e["kind"] == "tool_run"]
    check("the tool run is stored with name and full result",
          len(tr) == 1 and tr[0]["name"] == "get_battery" and isinstance(tr[0]["result"], dict), tr)
    check("every console line is in the log (command first, status last)",
          cons and cons[0]["kind"] == "command" and cons[-1]["kind"] == "status", [c["kind"] for c in cons])
    check("the log's console lines match the console store's, line for line",
          [c["seq"] for c in cons] == store_seqs, (cons, store_seqs))
    check("the reply event matches what the conversation file saved",
          [e for e in events if e["kind"] == "reply"][0]["text"] == saved[0]["jarvis"])
    check("rebuild_exchanges == the live file for a real ask",
          _norm(rebuilt) == _norm(saved), (rebuilt, saved))


def test_full_thinking_is_stored_even_though_the_saved_copy_is_clipped():
    long_thought = "step " * 400   # 2,000 chars
    saved_clip = {}
    orig_clip = ai_client.reasoning.clip_trace

    def tiny_clip(text, max_chars=None):
        saved_clip["called"] = True
        return text[:50] + " ...[clipped]"
    ai_client.reasoning.clip_trace = tiny_clip
    try:
        ai_providers.set_thinking("medium")
        with _ask_env() as conv:
            with _fake_http([
                {"stop_reason": "end_turn", "content": [
                    {"type": "thinking", "thinking": long_thought, "signature": "s"},
                    {"type": "text", "text": "done"}]},
            ]):
                r = ai_client.ask("think hard", commands=[], conversation_id=conv)
            events = raw_archive.read(conv, kinds=("model_reply",))
            saved = conversations.get_conversation(conv)["exchanges"][0]
    finally:
        ai_client.reasoning.clip_trace = orig_clip
        ai_providers.set_thinking("off")
    check("ask succeeded", r.ok, r)
    th = [e for e in saved.get("extras", []) if e.get("type") == "thinking"]
    if not th:
        check("provider produced a thinking trace (precondition)", False, saved.get("extras"))
        return
    check("the SAVED thinking extra is clipped", th[0]["data"]["text"].endswith("[clipped]"), th[0])
    full = (events[0].get("thinking") or {}).get("text", "") if events else ""
    check("the event log holds the FULL thinking text", full.strip() == long_thought.strip(), full[:80])


# ---- 3. rebuild ---------------------------------------------------------------

def test_rebuild_matches_live_file_past_the_60_cap():
    with _dirs():
        conv = _conv_id()
        conversations.begin_exchange(conv, "first")
        conversations.complete_exchange(conv, "first", "one", "t")
        _seed(conv, 64)
        live = conversations.get_conversation(conv)["exchanges"]
        rebuilt = raw_archive.rebuild_exchanges(conv)
        check("live file is capped at 60", len(live) == 60, len(live))
        check("rebuild has all 65", len(rebuilt) == 65, len(rebuilt))
        check("the newest 60 of the rebuild equal the live file", _norm(rebuilt[-60:]) == _norm(live))
        check("the oldest exchange survives byte-identical in the rebuild",
              rebuilt[0]["user"] == "first" and rebuilt[0]["jarvis"] == "one")
        check("unicode/emoji survive", rebuilt[5]["user"].startswith("q4 \u00e9\u4e2d\U0001F600"), rebuilt[5]["user"])


def test_rebuild_handles_redo_interrupt_and_unresolved():
    with _dirs():
        conv = _conv_id()
        conversations.append_exchange(conv, "keep", "k", "t")
        conversations.append_exchange(conv, "redo me", "old", "t")
        conversations.append_exchange(conv, "after", "later", "t")
        check("redo succeeds", conversations.drop_from_user(conv, "redo me"))
        conversations.begin_exchange(conv, "will be killed")
        conversations.abandon_exchange(conv, "will be killed", reason="interrupted")
        conversations.begin_exchange(conv, "never resolves")
        live = conversations.get_conversation(conv)["exchanges"]
        rebuilt = raw_archive.rebuild_exchanges(conv)
        check("redo removed 2 exchanges from the live file; rebuild agrees",
              _norm(rebuilt) == _norm(live), (_norm(rebuilt), _norm(live)))
        check("the interrupted turn rebuilds as interrupted", rebuilt[1]["interrupted"] and rebuilt[1]["user"] == "will be killed")
        check("the unresolved turn keeps what was said, marked pending",
              rebuilt[2]["user"] == "never resolves" and rebuilt[2].get("pending"))
        removed = raw_archive.read(conv, kinds=("exchange_removed",))
        check("what the redo removed is kept whole",
              [x["user"] for x in removed[0]["exchanges"]] == ["redo me", "after"], removed)


def test_a_hard_killed_turn_is_rebuilt_as_interrupted_when_the_next_one_starts():
    with _dirs():
        conv = _conv_id()
        conversations.begin_exchange(conv, "killed hard")          # never resolved
        conversations.begin_exchange(conv, "next question")        # reclaims the stale one
        conversations.complete_exchange(conv, "next question", "answer", "t")
        live = conversations.get_conversation(conv)["exchanges"]
        rebuilt = raw_archive.rebuild_exchanges(conv)
        check("live file downgraded the stale turn to interrupted", bool(live[0].get("interrupted")), live)
        check("rebuild matches the live file", _norm(rebuilt) == _norm(live), (_norm(rebuilt), _norm(live)))


def test_complete_without_begin_still_logs_the_user_text():
    with _dirs():
        conv = _conv_id()
        conversations.complete_exchange(conv, "no begin happened", "reply", "t")
        rebuilt = raw_archive.rebuild_exchanges(conv)
        check("user text recovered although no `user` event was written",
              [(e["user"], e["jarvis"]) for e in rebuilt] == [("no begin happened", "reply")], rebuilt)


# ---- 4 + 5. console ---------------------------------------------------------

def test_console_read_restores_a_clipped_line_from_the_log():
    with _dirs():
        conv = _conv_id()
        huge = "".join(chr(97 + i % 26) for i in range(console_store.MAX_LINE_CHARS + 1234))
        seq = console_store.append(conv, "tool-result", huge, turn="t1", tool="read_file")
        console_store.append(conv, "status", "short")
        stored = console_store._read_raw_lines(conv)[0]
        check("the console FILE is still clipped (store unchanged)",
              stored["text"].endswith("(1234 chars omitted)"), stored["text"][-40:])
        default = console_store.read(conv)["lines"]
        check("default read() is unchanged: still the clipped text",
              default[0]["text"].endswith("(1234 chars omitted)") and "restored" not in default[0])
        full = console_store.read(conv, full=True)["lines"]
        check("read(full=True) restores the full text", full[0]["text"] == huge and full[0].get("restored"), full[0]["text"][-40:])
        check("an unclipped line is untouched and not flagged", full[1]["text"] == "short" and "restored" not in full[1])
        check("seq ties the log entry to the line", raw_archive.console_full_text(conv)[seq] == huge)


def test_conversation_without_a_log_replays_exactly_as_before():
    with _dirs():
        conv = _conv_id()
        huge = "q" * (console_store.MAX_LINE_CHARS + 10)
        with _env("JARVIS_RAW_ARCHIVE", "0"):
            console_store.append(conv, "stdout", huge)
        got = console_store.read(conv, full=True)["lines"][0]
        check("no log: the clipped text is returned, nothing invented",
              got["text"].endswith("(10 chars omitted)") and "restored" not in got, got["text"][-30:])


def test_rebuild_console_includes_lines_the_size_cap_rotated_out():
    with _dirs():
        conv = _conv_id()
        orig = console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM
        console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM = 2000, 10
        try:
            for i in range(80):
                console_store.append(conv, "stdout", f"line {i} " + "y" * 40)
        finally:
            console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM = orig
        live = [l["text"] for l in console_store.read(conv, limit=0)["lines"]]
        check("the console file really was trimmed", not any(t.startswith("line 0 ") for t in live))
        rebuilt = [l["text"] for l in raw_archive.rebuild_console(conv)]
        check("rebuild_console has all 80 lines, in order",
              [t.split(" y")[0] for t in rebuilt] == [f"line {i}" for i in range(80)], rebuilt[:3])


# ---- 6. lifetime + safety ---------------------------------------------------

def test_clear_and_delete_remove_the_log():
    with _dirs():
        conv = _conv_id()
        _seed(conv, 3)
        check("log exists before clear", raw_archive.stats(conv)["entries"] > 0)
        conversations.clear(conv)
        check("Clear removes the log", not raw_archive._path(conv).exists())
        conv2 = _conv_id()
        _seed(conv2, 3)
        conversations.delete_conversation(conv2)
        check("delete removes the log", not raw_archive._path(conv2).exists())


def test_switch_off_and_never_raises():
    with _dirs():
        conv = _conv_id()
        with _env("JARVIS_RAW_ARCHIVE", "0"):
            check("disabled: record() writes nothing", raw_archive.record(conv, "user", {"text": "x"}) is False)
            _seed(conv, 3)
            check("disabled: no log file", not raw_archive._path(conv).exists())
            check("disabled: the conversation file is still written normally",
                  len(conversations.get_conversation(conv)["exchanges"]) == 3)
        check("a bad id is a silent no-op", raw_archive.record("../../etc", "user") is False)
        check("a bad id reads empty / zero stats",
              raw_archive.read("nope") == [] and raw_archive.stats("nope") == {"entries": 0, "bytes": 0})
        orig = raw_archive.ARCHIVE_DIR
        blocker = Path(tempfile.mkdtemp()) / "file"
        blocker.write_text("not a dir")
        raw_archive.ARCHIVE_DIR = blocker / "sub"
        try:
            check("an unwritable dir never raises", raw_archive.record(_conv_id(), "user") is False)
            conversations.append_exchange(_conv_id(), "still saves", "ok", "t")
            check("...and an exchange still saves", True)
        finally:
            raw_archive.ARCHIVE_DIR = orig


def test_torn_last_line_is_skipped():
    with _dirs():
        conv = _conv_id()
        raw_archive.record(conv, "user", {"text": "a"})
        with raw_archive._path(conv).open("a", encoding="utf-8") as f:
            f.write('{"v": 2, "kind": "us')
        check("a torn last line is skipped, earlier events survive",
              [e["text"] for e in raw_archive.read(conv)] == ["a"])


# ---- 7. real CLI --------------------------------------------------------------

def test_real_cli_conv_export_raw_and_console_read():
    # the CLI is a separate process, so it must see the same ~/.jarvis
    conv = _conv_id()
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=str(_JARVIS_CLI))
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from jarvis import conversations as c, console_store as s\n"
        "conv=%r\n"
        "c.begin_exchange(conv, 'typed  exactly')\n"
        "s.append(conv, 'tool-result', 'z'*(s.MAX_LINE_CHARS+500), turn='t1', tool='read_file')\n"
        "c.complete_exchange(conv, 'typed  exactly', 'the reply', 'test')\n"
    ) % (str(_JARVIS_CLI), conv)
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True, timeout=120)
    out = tempfile.mkdtemp(prefix="l38-out-")
    proc = subprocess.run([sys.executable, "-m", "jarvis", "conv-export", conv, "--raw", "--out", out],
                          capture_output=True, text=True, env=env, timeout=120, cwd=str(_JARVIS_CLI))
    check("conv-export --raw exits 0", proc.returncode == 0, proc.stderr[-300:] + proc.stdout[-300:])
    result = json.loads(proc.stdout)
    path = Path(result["path"])
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("a -raw.jsonl file with a meta line first",
          path.name.endswith("-raw.jsonl") and rows[0]["kind"] == "meta" and rows[0]["conversation"] == conv)
    check("meta counts the event kinds", rows[0]["counts"].get("user") == 1 and rows[0]["counts"].get("reply") == 1,
          rows[0]["counts"])
    check("the full-length console line is in the export",
          any(r["kind"] == "console" and len(r["text"]) == console_store.MAX_LINE_CHARS + 500 for r in rows))
    check("the typed text is exact in the export",
          any(r["kind"] == "user" and r["text"] == "typed  exactly" for r in rows))
    cr = subprocess.run([sys.executable, "-m", "jarvis", "console-read", conv, "--full"],
                        capture_output=True, text=True, env=env, timeout=120, cwd=str(_JARVIS_CLI))
    lines = json.loads(cr.stdout)["lines"]
    check("console-read --full restores the clipped line",
          any(len(l["text"]) == console_store.MAX_LINE_CHARS + 500 and l.get("restored") for l in lines))
    cc = subprocess.run([sys.executable, "-m", "jarvis", "console-read", conv],
                        capture_output=True, text=True, env=env, timeout=120, cwd=str(_JARVIS_CLI))
    check("plain console-read still returns the clipped text",
          any(l["text"].endswith("(500 chars omitted)") for l in json.loads(cc.stdout)["lines"]))
    both = subprocess.run([sys.executable, "-m", "jarvis", "console-read", conv, "--after-last-clear", "--full"],
                          capture_output=True, text=True, env=env, timeout=60, cwd=str(_JARVIS_CLI))
    check("console-read --after-last-clear --full terminates and restores (flag-loop regression)",
          both.returncode == 0 and any(l.get("restored") for l in json.loads(both.stdout)["lines"]),
          both.stderr[-200:])
    check("export_raw on an unknown conversation reports an error", conv_export.export_raw("0" * 16)["ok"] is False)


# ---- 8. unchanged ------------------------------------------------------------

def test_ui_facing_stores_did_not_change():
    check("MAX_STORED_EXCHANGES still 60", conversations.MAX_STORED_EXCHANGES == 60)
    check("console MAX_LINE_CHARS still 4000", console_store.MAX_LINE_CHARS == 4000)
    check("prompt truncation caps unchanged",
          conversations.MAX_USER_CHARS == 500 and conversations.MAX_ASSISTANT_CHARS == 700)


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
