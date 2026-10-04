"""Master plan L.38 (a) -- the raw archive (jarvis/raw_archive.py).

Pins the one promise the feature makes: whatever the capped stores throw away
is still on disk, byte for byte, and `conv-export --raw` can hand it back.

  1. Exchanges past the 60-exchange cap are archived whole (all three write
     paths: append_exchange, begin/complete_exchange), text untouched.
  2. A redo (`drop_from_user`) archives what it removed.
  3. A console line clipped at MAX_LINE_CHARS keeps its full text in the
     archive, tied to the clipped line by seq.
  4. Lines rotated out by the console size cap land in the archive.
  5. A tool call's FULL pre-shaping result is archived by the real executor.
  6. Clear and delete remove the archive; it never outlives either.
  7. JARVIS_RAW_ARCHIVE=0 writes nothing; a bad id / unwritable dir never raises.
  8. `conv-export --raw` (real CLI process) writes one chronological JSONL with
     every exchange, restored console text and tool runs, plus a meta line.
  9. What the model is shown is unchanged: the caps themselves did not move.

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

from jarvis import ai_client, console_store, conv_export, conversations, raw_archive  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _conv_id():
    return secrets.token_hex(8)


def _seed(conv, n, prefix="q"):
    for i in range(n):
        conversations.append_exchange(conv, f"{prefix}{i} \u00e9\u4e2d\U0001F600 " + "x" * 50, f"a{i}", "test")


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


# ---- 1. exchange overflow ---------------------------------------------------

def test_overflow_is_archived_whole():
    conv = _conv_id()
    _seed(conv, conversations.MAX_STORED_EXCHANGES + 5)
    kept = conversations.get_conversation(conv)["exchanges"]
    check("conversation file still holds exactly 60", len(kept) == 60, len(kept))
    arch = raw_archive.read(conv, kinds=("exchange_overflow",))
    dropped = [ex for e in arch for ex in e["exchanges"]]
    check("the 5 oldest exchanges are archived", len(dropped) == 5, len(dropped))
    check("archived text is byte-identical (unicode, emoji)",
          dropped[0]["user"] == "q0 \u00e9\u4e2d\U0001F600 " + "x" * 50, dropped[0]["user"])
    check("archived exchanges are the oldest, in order",
          [d["jarvis"] for d in dropped] == [f"a{i}" for i in range(5)], dropped)
    check("kept + archived = everything", len(kept) + len(dropped) == 65)


def test_overflow_via_begin_and_complete():
    conv = _conv_id()
    _seed(conv, 60)
    conversations.begin_exchange(conv, "pending one")
    over1 = [ex for e in raw_archive.read(conv, kinds=("exchange_overflow",)) for ex in e["exchanges"]]
    check("begin_exchange past the cap archives the oldest", len(over1) == 1 and over1[0]["jarvis"] == "a0", over1)
    conversations.complete_exchange(conv, "pending one", "done", "test")
    exs = conversations.get_conversation(conv)["exchanges"]
    check("complete_exchange keeps 60 and loses nothing",
          len(exs) == 60 and exs[-1]["jarvis"] == "done", len(exs))


def test_no_archive_when_under_cap():
    conv = _conv_id()
    _seed(conv, 10)
    check("nothing archived while under the cap", raw_archive.read(conv) == [])
    check("no archive file created", not raw_archive._path(conv).exists())


# ---- 2. redo ----------------------------------------------------------------

def test_redo_archives_removed_exchanges():
    conv = _conv_id()
    conversations.append_exchange(conv, "keep", "k", "test")
    conversations.append_exchange(conv, "redo me", "old answer", "test")
    conversations.append_exchange(conv, "after", "later", "test")
    check("drop_from_user succeeds", conversations.drop_from_user(conv, "redo me"))
    dropped = [ex for e in raw_archive.read(conv, kinds=("exchange_dropped",)) for ex in e["exchanges"]]
    check("the redone exchange and everything after it are archived",
          [d["user"] for d in dropped] == ["redo me", "after"], dropped)
    check("conversation itself is back to one exchange",
          len(conversations.get_conversation(conv)["exchanges"]) == 1)


# ---- 3 + 4. console ---------------------------------------------------------

@contextmanager
def _console_dir():
    orig = console_store.CONSOLE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        console_store.CONSOLE_DIR = Path(tmp)
        try:
            yield
        finally:
            console_store.CONSOLE_DIR = orig


def test_clipped_console_line_keeps_full_text():
    with _console_dir():
        conv = _conv_id()
        huge = "".join(chr(97 + i % 26) for i in range(console_store.MAX_LINE_CHARS + 1234))
        seq = console_store.append(conv, "tool-result", huge, turn="t1", tool="read_file")
        stored = console_store.read(conv)["lines"][0]
        check("console store still clips (UI behaviour unchanged)",
              stored["text"].endswith("(1234 chars omitted)") and len(stored["text"]) < len(huge))
        full = raw_archive.read(conv, kinds=("console_full",))
        check("archive holds the full text exactly", len(full) == 1 and full[0]["text"] == huge)
        check("archive entry is tied to the line by seq", full[0]["seq"] == seq == stored["seq"])
        console_store.append(conv, "status", "short")
        check("a short line is NOT duplicated into the archive",
              len(raw_archive.read(conv, kinds=("console_full",))) == 1)


def test_trimmed_console_lines_are_archived():
    with _console_dir():
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
        trimmed = [e["line"]["text"] for e in raw_archive.read(conv, kinds=("console_trimmed",))]
        check("line 0 is in the archive", any(t.startswith("line 0 ") for t in trimmed), trimmed[:3])
        check("no trim marker is archived as if it were output",
              not any(t.startswith("earlier output trimmed") for t in trimmed))
        every = {t.split(" y")[0] for t in trimmed + live if t.startswith("line ")}
        check("all 80 lines survive across store + archive", len(every) == 80, len(every))


# ---- 5. tool runs -----------------------------------------------------------

def test_tool_run_archived_with_full_result():
    conv = _conv_id()
    executor = ai_client._make_tool_executor(on_tool_call=None, schemas=None, conv_id=conv)
    executor("search_tools", {"query": "spotify"})
    runs = raw_archive.read(conv, kinds=("tool_run",))
    check("one tool_run archived", len(runs) == 1, runs)
    if runs:
        check("name and arguments are recorded",
              runs[0]["name"] == "search_tools" and runs[0]["arguments"] == {"query": "spotify"}, runs[0])
        check("the full result is recorded", bool((runs[0]["result"] or {}).get("matches")), runs[0]["result"])


def test_tool_run_without_conv_id_is_harmless():
    executor = ai_client._make_tool_executor(on_tool_call=None, schemas=None, conv_id=None)
    try:
        executor("search_tools", {"query": "spotify"})
        check("executor without a conversation id still works", True)
    except Exception as e:  # noqa: BLE001
        check("executor without a conversation id still works", False, e)


# ---- 6. lifetime ------------------------------------------------------------

def test_clear_and_delete_remove_the_archive():
    conv = _conv_id()
    _seed(conv, 62)
    check("archive exists before clear", raw_archive.stats(conv)["entries"] > 0)
    conversations.clear(conv)
    check("Clear removes the archive", not raw_archive._path(conv).exists())
    conv2 = _conv_id()
    _seed(conv2, 62)
    conversations.delete_conversation(conv2)
    check("delete removes the archive", not raw_archive._path(conv2).exists())


# ---- 7. switch + safety -----------------------------------------------------

def test_switch_off_and_never_raises():
    conv = _conv_id()
    with _env("JARVIS_RAW_ARCHIVE", "0"):
        check("disabled: record() writes nothing", raw_archive.record(conv, "tool_run", {"name": "x"}) is False)
        _seed(conv, 62)
        check("disabled: no archive file", not raw_archive._path(conv).exists())
        check("disabled: conversation still capped at 60 and intact",
              len(conversations.get_conversation(conv)["exchanges"]) == 60)
    check("a bad id is a silent no-op", raw_archive.record("../../etc", "tool_run") is False)
    check("a bad id reads empty", raw_archive.read("nope") == [] and raw_archive.stats("nope") == {"entries": 0, "bytes": 0})
    orig = raw_archive.ARCHIVE_DIR
    blocker = Path(tempfile.mkdtemp()) / "file"
    blocker.write_text("not a dir")
    raw_archive.ARCHIVE_DIR = blocker / "sub"  # mkdir under a file -> OSError
    try:
        check("an unwritable archive dir never raises", raw_archive.record(_conv_id(), "tool_run") is False)
    finally:
        raw_archive.ARCHIVE_DIR = orig


def test_half_written_last_line_is_skipped():
    conv = _conv_id()
    raw_archive.record(conv, "tool_run", {"name": "a"})
    with raw_archive._path(conv).open("a", encoding="utf-8") as f:
        f.write('{"v": 1, "kind": "tool_ru')
    check("a torn last line is skipped, earlier entries survive",
          [e["name"] for e in raw_archive.read(conv)] == ["a"])


# ---- 8. export --------------------------------------------------------------

def test_conv_export_raw_via_real_cli():
    conv = _conv_id()
    _seed(conv, conversations.MAX_STORED_EXCHANGES + 3)
    huge = "z" * (console_store.MAX_LINE_CHARS + 500)
    console_store.append(conv, "tool-result", huge, turn="t9", tool="read_file")
    ai_client._make_tool_executor(on_tool_call=None, schemas=None, conv_id=conv)(
        "search_tools", {"query": "spotify"})
    out = tempfile.mkdtemp(prefix="l38-out-")
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=str(_JARVIS_CLI))
    proc = subprocess.run([sys.executable, "-m", "jarvis", "conv-export", conv, "--raw", "--out", out],
                          capture_output=True, text=True, env=env, timeout=120, cwd=str(_JARVIS_CLI))
    check("CLI exits 0", proc.returncode == 0, proc.stderr[-300:] + proc.stdout[-300:])
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        check("CLI prints JSON", False, proc.stdout[-300:])
        return
    path = Path(result.get("path", ""))
    check("a -raw.jsonl file was written", path.name.endswith("-raw.jsonl") and path.exists(), result)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("first line is meta", rows[0]["kind"] == "meta" and rows[0]["conversation"] == conv, rows[0])
    exchanges = [r for r in rows if r["kind"] == "exchange"]
    check("all 63 exchanges present (60 kept + 3 archived)", len(exchanges) == 63, len(exchanges))
    check("archived ones are marked by source",
          sum(1 for r in exchanges if r["source"] == "exchange_overflow") == 3)
    consoles = [r for r in rows if r["kind"] == "console"]
    check("console line restored to full length",
          any(r["line"]["text"] == huge and r["line"].get("restored_from_archive") for r in consoles))
    check("tool run present with its full result",
          any(r["kind"] == "tool_run" and (r["result"] or {}).get("matches") for r in rows))
    stamps = [str(r.get("ts") or "") for r in rows[1:]]
    check("records are chronological", stamps == sorted(stamps))
    check("meta counts match", rows[0]["counts"].get("exchange") == 63, rows[0]["counts"])
    check("export_raw without a conversation reports an error",
          conv_export.export_raw("0" * 16)["ok"] is False)


# ---- 9. caps unchanged ------------------------------------------------------

def test_caps_did_not_move():
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
