"""Master plan L.38, the two follow-ups the raw-event-log build left open:

  A. the web UI replays a conversation from the event log, not only from the
     conversation file (``conv-show --replay`` -> raw_archive.replay_record,
     and ``console-read --full`` -> console_store's union with the log);
  B. the saved "extras" (screenshot / download / presented-file / organize-json
     / dev_agent cards, confirmations, a declined action) are no longer only
     computed at write time: thread_extras derives them from the logged tool
     runs, and a rebuild uses the same code the write path uses.

What is checked
  1. thread_extras: the moved functions behave as before (ai_client's wrappers
     delegate); rebuild_turn_extras re-derives tool cards, keeps the rest in
     order, and returns saved extras untouched when there are no runs.
  2. A real ask() (fake provider, real executor, one tool): the extras rebuilt
     from the log EQUAL the extras the write path saved.
  3. A multi-tool turn written through the same write functions: every card
     kind rebuilt from the log equals the saved one.
  4. The point of it: change what a card looks like in the derivation and old
     history changes with it; a card the file never had appears.
  5. replay_record: past the 60-cap; a log that begins after the conversation
     did; a log that disagrees with the file (not trusted); no log; a pending
     turn; a redo; an interrupted turn keeps what it did.
  6. console_store.read(full=True): clipped lines whole, rotated-out lines back,
     the "earlier output trimmed" marker dropped only when it is no longer
     true, since / kinds / surface / limit / after-last-clear still work, and
     the default read is byte-for-byte what it was.
  7. Real processes: `conv-show` (with and without --replay) and
     `console-read --full`.
  8. The web wiring (static): the server passes --replay / --full, the UI asks
     for full only on history loads, never on the poll.

Run: python3 tests/test_l38b_replay_from_log.py   (no key, no network)
"""

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l38b-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("JARVIS_RAW_ARCHIVE", None)

_ROOT = Path(__file__).resolve().parent.parent
_JARVIS_CLI = _ROOT / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis import (ai_client, ai_config, ai_providers, console_store,  # noqa: E402
                    conversations, logs, raw_archive, thread_extras)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:400]}")


def _conv_id():
    return secrets.token_hex(8)


@contextmanager
def _dirs():
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


@contextmanager
def _canned_tools(canned):
    """Replace the tool layer with fixed results for some names; everything
    else still runs for real."""
    orig = ai_client.system_tools.execute_tool

    def fake(name, arguments, context=None):
        if name in canned:
            return canned[name]
        return orig(name, arguments, context=context)

    ai_client.system_tools.execute_tool = fake
    try:
        yield
    finally:
        ai_client.system_tools.execute_tool = orig


@contextmanager
def _ask_env():
    orig = ai_config.load_ai_config
    cfg = {"persona": {}, "providers": [{"name": "anthropic", "type": "anthropic", "enabled": True,
                                         "api_keys": ["k1"], "model": "m"}],
           "defaults": {"tools_enabled": True, "prompt_mode": "full"}}
    with _dirs():
        ai_config.load_ai_config = lambda: cfg
        try:
            yield conversations.get_current_id()
        finally:
            ai_config.load_ai_config = orig


def _norm_extras(extras):
    """declinedAction.ts is time.time() at write time and the event's
    millisecond timestamp when rebuilt; everything else must match exactly.
    Returns (extras with ts zeroed, max ts difference)."""
    out, worst = [], 0.0
    for e in extras or []:
        if e.get("type") == "declinedAction":
            d = dict(e["data"])
            worst = max(worst, float(d.get("ts") or 0))
            d["ts"] = 0
            e = dict(e, data=d)
        out.append(e)
    return out, worst


def _types(extras):
    return [e.get("type") for e in extras or []]


def _only(extras, types):
    return [e for e in extras or [] if e.get("type") in types]


# ---- 1. thread_extras ---------------------------------------------------------

def test_thread_extras_moved_without_changing_behaviour():
    runs = [
        {"name": "take_screenshot", "arguments": {}, "result": {"ok": True, "file": "a.png"}},
        {"name": "organize_json", "arguments": {}, "result": {"ok": True, "path": "C:/d.json"}},
        {"name": "ytdl_download", "arguments": {}, "result": {
            "ok": True, "job_id": "j1", "files": [{"file": f"f{i}.mp4"} for i in range(8)]}},
        {"name": "take_screenshot", "arguments": {}, "result": {"ok": False, "file": "no.png"}},
    ]
    a, b = thread_extras.extras_from_runs(runs), ai_client._extras_from_runs(runs)
    check("ai_client._extras_from_runs delegates to thread_extras", a == b, (a, b))
    check("download cards still capped at five", len(_only(a, {"download"})) == 5, _types(a))
    check("a failed screenshot makes no card", len(_only(a, {"screenshot"})) == 1, _types(a))
    dec_runs = [{"name": "delete_path", "arguments": {"path": "x"}, "result": {"cancelled": True}}]
    d1, d2 = thread_extras.declined_action_extra(dec_runs, ts=5.0), ai_client._declined_action_extra(dec_runs)
    check("declined_action_extra: explicit ts is used, default is now",
          d1["data"]["ts"] == 5.0 and d2["data"]["ts"] > 1e9, (d1, d2))
    check("only the LAST call can be a declined action",
          thread_extras.declined_action_extra(dec_runs + [{"name": "x", "arguments": {}, "result": {"ok": True}}]) is None)


def test_rebuild_turn_extras_rules():
    runs = [{"name": "take_screenshot", "arguments": {}, "result": {"ok": True, "file": "new.png"}}]
    stored = [
        {"type": "screenshot", "data": {"filename": "OLD.png"}},
        {"type": "thinking", "data": {"text": "hm"}},
        {"type": "trace", "data": {"x": 1}},
        {"type": "consoleRef", "data": {"turn": "t", "lines": 3}},
    ]
    out = thread_extras.rebuild_turn_extras(runs, stored)
    check("tool cards are re-derived, the rest kept in saved order, after them",
          _types(out) == ["screenshot", "thinking", "trace", "consoleRef"], _types(out))
    check("the re-derived screenshot replaces the saved one", out[0]["data"]["filename"] == "new.png", out[0])
    check("no runs -> saved extras returned untouched",
          thread_extras.rebuild_turn_extras([], stored) == stored)
    check("no runs and nothing saved -> empty list", thread_extras.rebuild_turn_extras([], None) == [])
    check("tool-derived type list is exactly what extras_from_runs / declined can emit",
          thread_extras.TOOL_DERIVED_TYPES == {"confirm", "screenshot", "organizeJson", "download",
                                               "presentFile", "devAgent", "declinedAction"})


# ---- 2. real ask --------------------------------------------------------------

def test_real_ask_rebuilt_extras_equal_saved_extras():
    ai_providers.set_thinking("off")
    with _ask_env() as conv:
        with _canned_tools({"take_screenshot": {"ok": True, "file": "shot1.png"}}):
            with _fake_http([
                {"stop_reason": "tool_use", "content": [
                    {"type": "text", "text": "One moment."},
                    {"type": "tool_use", "id": "t1", "name": "take_screenshot", "input": {}}]},
                {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Taken."}]},
            ]):
                r = ai_client.ask("take a screenshot", commands=[], conversation_id=conv)
        saved = conversations.get_conversation(conv)["exchanges"]
        derived = raw_archive.rebuild_exchanges(conv, derive_extras=True)
        stored_copy = raw_archive.rebuild_exchanges(conv)
    check("ask succeeds and saved a screenshot card",
          r.ok and _types(_only(saved[0].get("extras"), {"screenshot"})) == ["screenshot"], _types(saved[0].get("extras")))
    check("extras rebuilt from the tool runs EQUAL the extras the write path saved (whole list)",
          _norm_extras(derived[0].get("extras"))[0] == _norm_extras(saved[0].get("extras"))[0],
          (derived[0].get("extras"), saved[0].get("extras")))
    check("the default rebuild still returns the reply event's own copy",
          stored_copy[0].get("extras") == saved[0].get("extras"))
    check("derive_extras changes nothing but extras",
          [(e["user"], e["jarvis"], e["provider"]) for e in derived]
          == [(e["user"], e["jarvis"], e["provider"]) for e in saved])


# ---- 3. every card kind, written through the same write functions -------------

_MULTI_RUNS = [
    {"name": "take_screenshot", "arguments": {}, "result": {"ok": True, "file": "s.png"},
     "confirm": {"approved": True, "risk_note": "sees your screen"}},
    {"name": "organize_json", "arguments": {"path": "C:/d.json"}, "result": {"ok": True, "path": "C:/d.json"}},
    {"name": "ytdl_download", "arguments": {"url": "u"}, "result": {
        "ok": True, "job_id": "j1", "files": [{"file": "a.mp4", "title": "A"}, {"file": "b.mp4"}]}},
    {"name": "present_file", "arguments": {}, "result": {
        "ok": True, "job_id": "j2", "download_filename": "r.pdf", "name": "r.pdf",
        "size_bytes": 12, "path": "C:/r.pdf"}},
    {"name": "dev_agent", "arguments": {}, "result": {
        "ok": True, "job_id": "j3", "project_dir": "C:/p",
        "steps": [{"kind": "run", "preview": "p" * 5000, "stdout_tail": "out"}]}},
    {"name": "delete_path", "arguments": {"path": "x"}, "result": {"cancelled": True},
     "confirm": {"approved": False, "risk_note": "deletes"}},
]


def _write_turn_like_ask(conv, user, runs, reply="ok", extra_tail=None):
    """The same calls ask() makes for a finished turn: begin, one tool_run event
    per run, extras from the runs (+ declined), the other extras, complete."""
    conversations.begin_exchange(conv, user)
    for run in runs:
        raw_archive.record(conv, "tool_run", {"name": run["name"], "arguments": run["arguments"],
                                              "result": run["result"], "confirm": run.get("confirm")})
    extras = ai_client._extras_from_runs(runs)
    declined = ai_client._declined_action_extra(runs)
    if declined:
        extras.append(declined)
    extras += extra_tail or [{"type": "trace", "data": {"rounds": 2}},
                             {"type": "consoleRef", "data": {"turn": "t1", "lines": 4}}]
    conversations.complete_exchange(conv, user, reply, "test", extras=extras)


def test_every_card_kind_rebuilds_equal_to_what_was_saved():
    with _dirs():
        conv = _conv_id()
        _write_turn_like_ask(conv, "do everything", _MULTI_RUNS)
        saved = conversations.get_conversation(conv)["exchanges"][0]["extras"]
        derived = raw_archive.rebuild_exchanges(conv, derive_extras=True)[0]["extras"]
    check("saved extras contain every card kind",
          set(_types(saved)) >= {"confirm", "screenshot", "organizeJson", "download", "presentFile",
                                 "devAgent", "declinedAction", "trace", "consoleRef"}, _types(saved))
    d_norm, d_ts = _norm_extras(derived)
    s_norm, s_ts = _norm_extras(saved)
    check("rebuilt extras EQUAL saved extras, in the same order (declined ts aside)",
          d_norm == s_norm, (d_norm, s_norm))
    check("the declined action's ts is the time the call ran, within a few ms of write time",
          abs(d_ts - s_ts) < 0.5, (d_ts, s_ts))
    dev = _only(derived, {"devAgent"})[0]["data"]
    check("the dev_agent steps come back at full size (the log holds the pre-shaping result)",
          len(dev["steps"][0]["preview"]) == 5000)


# ---- 4. the point: the derivation can change and history follows ---------------

def test_changing_the_derivation_changes_old_history():
    with _dirs():
        conv = _conv_id()
        _write_turn_like_ask(conv, "shot", [_MULTI_RUNS[0]])
        # a conversation file saved by an OLDER derivation: no screenshot card at all
        rec = conversations.get_conversation(conv)
        rec["exchanges"][0]["extras"] = [e for e in rec["exchanges"][0]["extras"] if e["type"] != "screenshot"]
        conversations._save_conv(rec)
        before = _types(conversations.get_conversation(conv)["exchanges"][0]["extras"])
        replayed, info = raw_archive.replay_record(conv)
        after = _types(replayed["exchanges"][0]["extras"])
        orig = thread_extras.extras_from_runs

        def newer(runs):
            out = orig(runs)
            for e in out:
                if e["type"] == "screenshot":
                    e["data"]["alt"] = "a card kind the old writer never knew"
            return out

        thread_extras.extras_from_runs = newer
        try:
            changed = raw_archive.replay_record(conv)[0]["exchanges"][0]["extras"]
        finally:
            thread_extras.extras_from_runs = orig
    check("the file lost the card (older derivation)", "screenshot" not in before, before)
    check("replay brings the card back from the logged tool run", "screenshot" in after, after)
    check("replay uses the current derivation (a new field shows on old history)",
          any(e["type"] == "screenshot" and e["data"].get("alt") for e in changed), changed)
    check("source is the event log", info["source"] == "events", info)


# ---- 5. replay_record -----------------------------------------------------------

def _seed(conv, n, start=0):
    for i in range(start, start + n):
        _write_turn_like_ask(conv, f"q{i} \u00e9\u4e2d", [], reply=f"a{i}", extra_tail=[])


def test_replay_goes_past_the_60_cap():
    with _dirs():
        conv = _conv_id()
        _seed(conv, 75)
        file_n = len(conversations.get_conversation(conv)["exchanges"])
        rec, info = raw_archive.replay_record(conv)
    check("the file is capped at 60", file_n == 60, file_n)
    check("replay returns all 75", len(rec["exchanges"]) == 75, len(rec["exchanges"]))
    check("oldest and newest are right",
          rec["exchanges"][0]["user"].startswith("q0 ") and rec["exchanges"][-1]["jarvis"] == "a74")
    check("info: events, 75 from the log, none from the file",
          info["source"] == "events" and info["from_log"] == 75 and info["from_file"] == 0
          and info["file_exchanges"] == 60, info)
    check("the rest of the record (id, title) is the file's", rec["id"] == conv and "title" in rec)


def test_a_log_that_begins_after_the_conversation_keeps_the_older_file_exchanges():
    with _dirs():
        conv = _conv_id()
        with _env("JARVIS_RAW_ARCHIVE", "0"):
            _seed(conv, 10)                       # before the event log existed
        _seed(conv, 3, start=10)
        rec, info = raw_archive.replay_record(conv)
    check("13 exchanges: 10 from the file in front of 3 from the log", len(rec["exchanges"]) == 13, info)
    check("order is preserved", [e["jarvis"] for e in rec["exchanges"]] == [f"a{i}" for i in range(13)])
    check("info counts both", info["from_file"] == 10 and info["from_log"] == 3, info)


def test_a_log_that_disagrees_with_the_file_is_not_trusted():
    with _dirs():
        conv = _conv_id()
        _seed(conv, 5)
        rec = conversations.get_conversation(conv)
        rec["exchanges"][-1]["jarvis"] = "someone edited this"
        conversations._save_conv(rec)
        out, info = raw_archive.replay_record(conv)
    check("falls back to the conversation file", info["source"] == "conversation-file", info)
    check("and says why", "does not match" in info["reason"], info)
    check("the file's record is returned unchanged", out["exchanges"][-1]["jarvis"] == "someone edited this")


def test_no_log_means_the_file_exactly():
    with _dirs():
        conv = _conv_id()
        with _env("JARVIS_RAW_ARCHIVE", "0"):
            _seed(conv, 4)
        out, info = raw_archive.replay_record(conv)
        file_rec = conversations.get_conversation(conv)
        missing = raw_archive.replay_record("0" * 16)
        bad = raw_archive.replay_record("../../etc")
    check("source is the conversation file, reason 'no event log'",
          info["source"] == "conversation-file" and info["reason"] == "no event log", info)
    check("record identical to the file's", out == file_rec)
    check("unknown / invalid conversation -> (None, source none)",
          missing[0] is None and missing[1]["source"] == "none" and bad[0] is None)


def test_pending_redo_and_interrupted():
    with _dirs():
        conv = _conv_id()
        _seed(conv, 3)
        conversations.begin_exchange(conv, "still running")
        rec, info = raw_archive.replay_record(conv)
        pend_ok = rec["exchanges"][-1].get("pending") is True and rec["exchanges"][-1]["user"] == "still running"
        conversations.complete_exchange(conv, "still running", "done", "test")
        conversations.drop_from_user(conv, "q1 \u00e9\u4e2d")
        file_after_redo = [e["user"] for e in conversations.get_conversation(conv)["exchanges"]]
        rec2, info2 = raw_archive.replay_record(conv)
    check("a turn in flight replays as pending with what the person typed", pend_ok, info)
    check("a redo removes exactly what the file removed",
          [e["user"] for e in rec2["exchanges"]] == file_after_redo and info2["source"] == "events", info2)

    with _dirs():
        conv = _conv_id()
        conversations.begin_exchange(conv, "take a shot then die")
        raw_archive.record(conv, "tool_run", {"name": "take_screenshot", "arguments": {},
                                              "result": {"ok": True, "file": "before-kill.png"}})
        conversations.abandon_exchange(conv, "take a shot then die", reason="stopped")
        file_ex = conversations.get_conversation(conv)["exchanges"][0]
        rec, info = raw_archive.replay_record(conv)
    check("an interrupted turn is still interrupted", rec["exchanges"][0].get("interrupted") == "stopped", rec["exchanges"][0])
    check("the card for what it did before it stopped is rebuilt from the log (the file has none)",
          _types(rec["exchanges"][0].get("extras")) == ["screenshot"] and not file_ex.get("extras"),
          (rec["exchanges"][0].get("extras"), file_ex.get("extras")))


# ---- 6. console_store.read(full=True) ------------------------------------------

@contextmanager
def _small_console_caps(max_bytes, keep):
    old = (console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM)
    console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM = max_bytes, keep
    try:
        yield
    finally:
        console_store.MAX_FILE_BYTES, console_store.KEEP_LINES_ON_TRIM = old


def test_console_full_recovers_rotated_out_and_clipped_lines():
    with _dirs():
        conv = _conv_id()
        with _small_console_caps(4000, 10):
            for i in range(80):
                console_store.append(conv, "stdout", f"line {i} " + "y" * 60, surface="live", turn="t1")
            console_store.append(conv, "tool-result", "Z" * (console_store.MAX_LINE_CHARS + 700),
                                 surface="ask", turn="t2", tool="read_file")
        plain = console_store.read(conv, limit=0)
        full = console_store.read(conv, limit=0, full=True)
        plain_again = console_store.read(conv, limit=0, full=False)
    plain_texts = [l["text"] for l in plain["lines"]]
    full_texts = [l["text"] for l in full["lines"]]
    check("the default read lost the old lines to rotation (precondition)",
          not any(t.startswith("line 0 ") for t in plain_texts) and len(plain["lines"]) < 30, len(plain["lines"]))
    check("the default read still carries the store's trim marker",
          any(t.startswith("earlier output trimmed") for t in plain_texts))
    check("full has all 81 console lines back", len(full["lines"]) == 81, len(full["lines"]))
    check("full is in seq order, oldest first",
          [l["seq"] for l in full["lines"]] == sorted(l["seq"] for l in full["lines"]))
    check("rotated-out lines are marked recovered", any(l.get("recovered") for l in full["lines"]))
    check("the trim marker is dropped once the lines it apologises for are back",
          not any(t.startswith("earlier output trimmed") for t in full_texts))
    big = [l for l in full["lines"] if l.get("tool") == "read_file"][0]
    check("the clipped line is whole again and marked restored",
          len(big["text"]) == console_store.MAX_LINE_CHARS + 700 and big.get("restored"), len(big["text"]))
    check("the default read is byte-for-byte unchanged by all this (a full read leaves nothing behind)",
          plain == plain_again)


def test_console_full_keeps_the_read_options_working():
    with _dirs():
        conv = _conv_id()
        console_store.append(conv, "stdout", "old live", surface="live", turn="a")
        console_store.append(conv, "tool-call", "ask call", surface="ask", turn="b", tool="x")
        marker_seq = console_store.clear_marker(conv)
        console_store.append(conv, "stdout", "after clear", surface="live", turn="c")
        console_store.append(conv, "stdout", "after clear 2", surface="live", turn="c")
        base = dict(limit=2000, full=True)
        live = console_store.read(conv, surface="live", **base)["lines"]
        after = console_store.read(conv, surface="live", after_last_clear=True, **base)
        kinds = console_store.read(conv, kinds=["tool-call"], **base)["lines"]
        turn_c = console_store.read(conv, turn="c", **base)["lines"]
        since = console_store.read(conv, since_seq=marker_seq, **base)["lines"]
        lim = console_store.read(conv, limit=2, full=True)
    check("surface filter", [l["text"] for l in live if l["kind"] == "stdout"] == ["old live", "after clear", "after clear 2"])
    check("after_last_clear hides everything up to the marker, with full on",
          [l["text"] for l in after["lines"]] == ["after clear", "after clear 2"] and after["cleared_through_seq"] == marker_seq,
          after)
    check("kinds filter", [l["text"] for l in kinds] == ["ask call"])
    check("turn filter", [l["text"] for l in turn_c] == ["after clear", "after clear 2"])
    check("since_seq filter", [l["text"] for l in since] == ["after clear", "after clear 2"])
    check("limit keeps the newest and reports truncation",
          [l["text"] for l in lim["lines"]] == ["after clear", "after clear 2"] and lim["truncated"])


def test_console_full_without_a_log_is_exactly_the_store():
    with _dirs():
        conv = _conv_id()
        with _env("JARVIS_RAW_ARCHIVE", "0"):
            console_store.append(conv, "stdout", "w" * (console_store.MAX_LINE_CHARS + 50), surface="live", turn="t")
            console_store.append(conv, "stdout", "short", surface="live", turn="t")
        plain = console_store.read(conv)
        full = console_store.read(conv, full=True)
        empty_full = console_store.read(_conv_id(), full=True)
    check("no log: full == plain", plain == full, (plain, full))
    check("nothing at all: still reports legacy so the CLI falls back", empty_full["legacy"] is True and empty_full["lines"] == [])


# ---- 7. real processes -------------------------------------------------------------

def test_real_cli_conv_show_replay_and_console_read_full():
    conv = _conv_id()
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=str(_JARVIS_CLI))
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from jarvis import conversations as c, console_store as s, raw_archive as r\n"
        "conv=%r\n"
        "for i in range(65):\n"
        "    c.begin_exchange(conv, 'q%%d' %% i)\n"
        "    if i == 64:\n"
        "        r.record(conv, 'tool_run', {'name': 'take_screenshot', 'arguments': {}, 'result': {'ok': True, 'file': 'cli.png'}})\n"
        "    c.complete_exchange(conv, 'q%%d' %% i, 'a%%d' %% i, 'test')\n"
        "s.append(conv, 'tool-result', 'z'*(s.MAX_LINE_CHARS+500), surface='live', turn='t1', tool='read_file')\n"
    ) % (str(_JARVIS_CLI), conv)
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True, timeout=180)

    def run(*args):
        return subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True,
                              env=env, timeout=120, cwd=str(_JARVIS_CLI))

    plain = run("conv-show", conv)
    replay = run("conv-show", conv, "--replay")
    check("conv-show (no flag) exits 0 and is the file: 60 exchanges, no `replay` key",
          plain.returncode == 0 and len(json.loads(plain.stdout)["exchanges"]) == 60
          and "replay" not in json.loads(plain.stdout), plain.stderr[-300:])
    r = json.loads(replay.stdout)
    check("conv-show --replay exits 0 and returns all 65", replay.returncode == 0 and len(r["exchanges"]) == 65, replay.stderr[-300:])
    check("conv-show --replay says it came from the events",
          r["replay"]["source"] == "events" and r["replay"]["from_log"] == 65, r.get("replay"))
    check("the screenshot card is on the exchange the tool ran in",
          _types(r["exchanges"][64].get("extras")) == ["screenshot"], r["exchanges"][64])
    check("conv-show --replay on an unknown id still errors like before",
          run("conv-show", "0" * 16, "--replay").returncode == 1)
    full = run("console-read", conv, "--full", "--surface", "live")
    lines = json.loads(full.stdout)["lines"]
    check("console-read --full restores the clipped line",
          full.returncode == 0 and any(len(l["text"]) == console_store.MAX_LINE_CHARS + 500 for l in lines), full.stderr[-300:])


# ---- 8. the web wiring (static; the live check is the .js file next to this) -------

def test_web_wiring():
    server = (_ROOT / "web" / "server.js").read_text(encoding="utf-8")
    app = (_ROOT / "web" / "public" / "app.js").read_text(encoding="utf-8")
    m = re.search(r'app\.get\("/api/conversations/:id".*?\n\}\);', server, re.S)
    check("GET /api/conversations/:id asks conv-show for --replay unless ?source=file",
          m and '"--replay"' in m.group(0) and 'source !== "file"' in m.group(0))
    m = re.search(r'app\.get\("/api/console/:id".*?\n\}\);', server, re.S)
    check("GET /api/console/:id passes --full only for ?full=1",
          m and '"--full"' in m.group(0) and 'req.query.full === "1"' in m.group(0))
    check("Api.getConsole sends full=1 when asked", 'params.set("full", "1")' in app)
    full_calls = re.findall(r'Api\.getConsole\([^)]*full: true', app)
    check("exactly the two history loads (Ask trace, Live output) ask for full", len(full_calls) == 2, full_calls)
    poll = re.search(r'since: subagentLiveSeq\[id\][^)]*\)', app)
    check("the ~1 s subagent poll does not ask for full", poll and "full" not in poll.group(0), poll and poll.group(0))


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
