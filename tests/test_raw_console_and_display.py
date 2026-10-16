"""The Ask console and the conversation thread are SAVED AS SHOWN.

Before this, a reload rebuilt both from records of what happened: the console
from described events ("$ trying  groq (key 1/4)", "answered"), the thread's
cards from the turn's tool runs. This pins the other direction.

  1. console_store.append_trace saves console lines exactly as painted: text,
     colour class and order, a clipped line whole again with read(full=True),
     and the described events next to them are untouched.
  2. The console-append-trace CLI command (what POST /api/console/:id/trace runs)
     round-trips through a real process, refuses a bad id / bad payload.
  3. conversations.save_display keeps the cards a turn drew, as drawn, on the
     newest finished exchange (page bookkeeping dropped), refuses what is too big
     or has nowhere to go, and a page-less / old exchange is unchanged.
  4. The event log carries it: rebuild_exchanges / replay_record hand the
     display back, and a log from before this existed replays as it always did.
  5. The conv-save-display CLI command round-trips through a real process.
  6. Both new verbs are reserved command names.

Run: python3 tests/test_raw_console_and_display.py   (no key, no network)
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-rawdisplay-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("JARVIS_RAW_ARCHIVE", None)

_JARVIS_CLI = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis import (console_store, conversations, logs, raw_archive,  # noqa: E402
                    reserved_names)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


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


# The exact lines from the real console this feature was asked for from.
SAMPLE = [
    ("$ jarvis", "cmd"),
    ("[tools] Auto-discovered ['calendar_add_event', 'calendar_delete_event'] from calendar_tools.py (group='calendar')", "sys"),
    ("[tools] Note: code_agent.py (group='dev_agent'): ['edit_file', 'read_file'] have no TOOL_KEYWORDS of their own", "sys"),
    ("\u21b3 asking groq (key 1/4)\u2026", "sys"),
    ("tokens: in=2235 out=1109 total=3344 rounds=1 tools=0", "tool"),
    ("$ tokens in=2235 out=1109 total=3344 rounds=1 tools=0", "tool"),
    ("done", "done"),
]


def _lines(conv, **kw):
    return console_store.read(conv, surface="ask", **kw)["lines"]


# ---------------------------------------------------------------- 1. the store
with _dirs():
    conv = conversations.get_current_id()
    # A described event first, like ai_client writes during the same turn.
    console_store.append(conv, "command", "jarvis", surface="ask", turn="t1")
    console_store.append(conv, "provider", "groq (key 1/4)", surface="ask", turn="t1")
    n = console_store.append_trace(conv, "b1", [{"text": t, "cls": c} for t, c in SAMPLE])
    console_store.append(conv, "status", "answered", surface="ask", turn="t1")
    check("append_trace reports every line written", n == len(SAMPLE), n)

    trace = [l for l in _lines(conv) if l["kind"] == "trace"]
    check("trace lines come back in the order they were painted",
          [l["text"] for l in trace] == [t for t, _ in SAMPLE], [l["text"] for l in trace][:2])
    check("each trace line keeps the colour class it was painted with",
          [l["cls"] for l in trace] == [c for _, c in SAMPLE], [l["cls"] for l in trace])
    check("trace lines carry the page's own turn id", {l["turn"] for l in trace} == {"b1"})
    check("trace lines are on the ask surface", all(l["surface"] == "ask" for l in trace))

    kinds = [l["kind"] for l in _lines(conv)]
    check("the described events are still there, untouched",
          kinds.count("command") == 1 and kinds.count("provider") == 1 and kinds.count("status") == 1, kinds)
    only_trace = _lines(conv, kinds=["trace"])
    check("kinds=['trace'] returns only the raw lines", len(only_trace) == len(SAMPLE))
    non_trace = _lines(conv, kinds=["command", "provider", "tool-call", "tool-result", "error", "status"])
    check("the summary kinds can be read without the raw lines", len(non_trace) == 3 and all(l["kind"] != "trace" for l in non_trace))
    live = console_store.read(conv, surface="live")["lines"]
    check("nothing leaks onto the live surface", not live, live)

    # A long line: the store clips its copy, full=True gives the whole line back.
    long_text = "[tools] " + "x" * (console_store.MAX_LINE_CHARS + 500)
    console_store.append_trace(conv, "b2", [{"text": long_text, "cls": "sys"}])
    clipped = [l for l in _lines(conv, kinds=["trace"]) if l["text"].startswith("[tools] x")]
    whole = [l for l in _lines(conv, kinds=["trace"], full=True) if l["text"].startswith("[tools] x")]
    check("the store's own copy of a very long line is clipped", clipped and len(clipped[0]["text"]) < len(long_text))
    check("read(full=True) restores the long raw line whole", whole and whole[0]["text"] == long_text)

    # Rotated-out raw lines come back with their colour class from the raw log.
    events = raw_archive.rebuild_console(conv)
    ev_trace = [l for l in events if l.get("kind") == "trace"]
    check("the raw event log keeps the colour class too", ev_trace and ev_trace[0].get("cls") == SAMPLE[0][1], ev_trace[:1])

    # Hostile input: never raises, never writes junk.
    check("append_trace with a bad id writes nothing", console_store.append_trace("../x", "b", [{"text": "a"}]) == 0)
    check("append_trace with a non-list writes nothing", console_store.append_trace(conv, "b", "nope") == 0)
    check("append_trace skips a line with no text",
          console_store.append_trace(conv, "b3", [{"cls": "sys"}, {"text": "kept"}]) == 1)
    check("a bare string is taken as a plain line",
          console_store.append_trace(conv, "b4", ["plain"]) == 1
          and [l for l in _lines(conv, kinds=["trace"]) if l["text"] == "plain"][0]["cls"] == "sys")

# ---------------------------------------------------------------- 2. the CLI
def _cli(args, stdin=None, home=None):
    env = dict(os.environ, HOME=home or _HOME, USERPROFILE=home or _HOME, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], input=stdin, capture_output=True,
                       text=True, cwd=str(_JARVIS_CLI), env=env, timeout=120)
    return p.returncode, p.stdout, p.stderr


conv_cli = _conv_id()
rc, out, err = _cli(["console-append-trace", conv_cli],
                    json.dumps({"turn": "b9", "lines": [{"text": t, "cls": c} for t, c in SAMPLE]}))
check("console-append-trace exits 0 and says how many it wrote",
      rc == 0 and json.loads(out.strip().splitlines()[-1]).get("written") == len(SAMPLE), (rc, out[-200:], err[-200:]))
rc, out, err = _cli(["console-read", conv_cli, "--surface", "ask", "--kinds", "trace", "--full"])
got = json.loads(out)["lines"] if rc == 0 else []
check("console-read gives the same lines back through a real process",
      [(l["text"], l["cls"]) for l in got] == SAMPLE, (rc, err[-200:]))
rc, out, _ = _cli(["console-append-trace", "not a valid id!"], "{}")
check("console-append-trace refuses a bad conversation id", rc != 0 and "invalid" in out, (rc, out))
rc, out, _ = _cli(["console-append-trace", conv_cli], "{nope")
check("console-append-trace refuses a malformed payload", rc != 0 and "malformed" in out, (rc, out))

# -------------------------------------------------------------- 3. save_display
CARDS = [
    {"type": "screenshot", "data": {"filename": "shot.png", "jobId": "j1"}, "bucket": 0},
    {"type": "confirm", "data": {"tool": "run_command", "resolved": "approved"}},
    {"type": "uiBubble", "data": {"title": "Heads up", "body": "from a tool", "level": "warn"}},
    {"type": "devAgent", "data": {"jobId": "d1", "steps": [{"n": 1, "label": "edit"}], "dom": "<node>"}},
    {"type": "organizeJson", "data": {"targetPath": "C:/x.json", "payload": {"huge": "x" * 1000}}},
]
with _dirs():
    conv = conversations.get_current_id()
    conversations.append_exchange(conv, "first question", "first answer", "t")
    conversations.append_exchange(conv, "second question", "second answer", "t")

    res = conversations.save_display(conv, None, CARDS)
    check("save_display reports ok and the item count", res == {"ok": True, "items": 5}, res)
    rec = conversations.get_conversation(conv)
    first, second = rec["exchanges"]
    check("it lands on the NEWEST finished exchange", "display" in second and "display" not in first)
    saved = second["display"]
    check("every card is kept, in the order it was drawn", [c["type"] for c in saved] ==
          ["screenshot", "confirm", "uiBubble", "devAgent", "organizeJson"], [c["type"] for c in saved])
    check("a card's data is kept exactly as drawn (a resolved confirm stays resolved)",
          saved[1]["data"] == {"tool": "run_command", "resolved": "approved"})
    check("a devAgent card keeps its steps", saved[3]["data"]["steps"] == [{"n": 1, "label": "edit"}])
    check("the page's own bookkeeping (dom reference, bucket) is not saved",
          "dom" not in saved[3]["data"] and all(set(c) == {"type", "data"} for c in saved))
    check("an organize-json payload is left out (it is re-read from the file)", "payload" not in saved[4]["data"])
    check("the exchange's text is untouched", second["user"] == "second question" and second["jarvis"] == "second answer")

    res = conversations.save_display(conv, "first question", [{"type": "download", "data": {"filename": "f.zip"}}])
    rec = conversations.get_conversation(conv)
    check("a user text picks that turn instead of the newest",
          res.get("ok") and rec["exchanges"][0]["display"][0]["type"] == "download" and len(rec["exchanges"][1]["display"]) == 5, res)

    res = conversations.save_display(conv, "never asked", CARDS)
    check("no matching exchange: refused, nothing changed", res["ok"] is False)
    res = conversations.save_display(conv, None, "not a list")
    check("items that are not a list are refused", res["ok"] is False)
    res = conversations.save_display("../bad", None, CARDS)
    check("a bad conversation id is refused", res["ok"] is False)
    res = conversations.save_display(_conv_id(), None, CARDS)
    check("a conversation that does not exist is refused", res["ok"] is False and "no such" in res["reason"], res)
    big = [{"type": "console", "data": {"dumpLines": ["x" * 1000] * 200}}]
    res = conversations.save_display(conv, None, big)
    check("something too big to keep is refused rather than bloating the file",
          res["ok"] is False and "too large" in res["reason"], res)
    check("a refused save leaves the earlier display in place",
          len(conversations.get_conversation(conv)["exchanges"][1]["display"]) == 5)
    res = conversations.save_display(conv, None, [{"type": "bad type!", "data": {}}, {"type": "ok", "data": {}}, "junk", None])
    check("malformed items are skipped, well-formed ones kept", res == {"ok": True, "items": 1}, res)
    res = conversations.save_display(conv, None, [])
    check("an empty list clears the display (the exchange falls back to its derived cards)",
          res["ok"] is True and "display" not in conversations.get_conversation(conv)["exchanges"][1])

    # A pending exchange is never the target.
    conversations.begin_exchange(conv, "still running")
    res = conversations.save_display(conv, None, CARDS)
    rec = conversations.get_conversation(conv)
    check("a still-running (pending) exchange is skipped; the newest FINISHED one gets it",
          res.get("ok") and "display" in rec["exchanges"][1] and "display" not in rec["exchanges"][-1], res)

# ---------------------------------------------------- 4. the event log + replay
with _dirs():
    conv = conversations.get_current_id()
    conversations.append_exchange(conv, "hello", "hi there", "t")
    conversations.append_exchange(conv, "take a screenshot", "done", "t",
                                  extras=[{"type": "screenshot", "data": {"filename": "derived.png"}},
                                          {"type": "thinking", "data": {"text": "hmm", "rounds": 1}}])
    conversations.save_display(conv, None, [
        {"type": "screenshot", "data": {"filename": "drawn.png"}},
        {"type": "uiBubble", "data": {"title": "t", "body": "b", "level": "info"}}])
    rebuilt = raw_archive.rebuild_exchanges(conv, derive_extras=True)
    check("the event log rebuilds the display onto the same exchange",
          len(rebuilt) == 2 and [c["type"] for c in rebuilt[1].get("display", [])] == ["screenshot", "uiBubble"]
          and "display" not in rebuilt[0], [list(e) for e in rebuilt])
    check("the drawn card (not the derived one) is what the display holds",
          rebuilt[1]["display"][0]["data"]["filename"] == "drawn.png")
    record, info = raw_archive.replay_record(conv)
    check("replay_record carries the display through to the web UI",
          record and [c["type"] for c in record["exchanges"][1].get("display", [])] == ["screenshot", "uiBubble"], info)
    check("replay_record still carries the derived extras alongside, for the cards the page does not record",
          any(e["type"] == "thinking" for e in record["exchanges"][1].get("extras", [])),
          record["exchanges"][1].get("extras"))

with _dirs():   # a conversation from before this existed: no display anywhere
    conv = conversations.get_current_id()
    conversations.append_exchange(conv, "old turn", "old answer", "t")
    record, info = raw_archive.replay_record(conv)
    check("a conversation with no display replays exactly as before",
          record and "display" not in record["exchanges"][0] and info["source"] in ("events", "conversation-file"), info)

# ------------------------------------------------------ 5. the other CLI command
conv_cli2 = _conv_id()
# CLI processes use the real (scratch-HOME) stores, so build the conversation there too.
code = (
    "import sys; sys.path.insert(0, %r)\n"
    "from jarvis import conversations\n"
    "cid = conversations.new_conversation(title='t', make_current=False)\n"
    "cid = cid if isinstance(cid, str) else cid.get('id')\n"
    "conversations.append_exchange(cid, 'q', 'a', 't')\n"
    "print(cid)\n"
) % str(_JARVIS_CLI)
p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                   env=dict(os.environ, HOME=_HOME, USERPROFILE=_HOME), timeout=120)
cid = (p.stdout.strip().splitlines() or [""])[-1]
rc, out, err = _cli(["conv-save-display", cid], json.dumps({"items": CARDS}))
check("conv-save-display saves through a real process",
      rc == 0 and json.loads(out.strip().splitlines()[-1]) == {"ok": True, "items": 5}, (rc, out[-200:], err[-300:], p.stderr[-200:]))
rc, out, err = _cli(["conv-show", cid])
check("conv-show returns the saved display", '"display"' in out and "uiBubble" in out, (rc, out[:200], err[-200:]))
rc, out, _ = _cli(["conv-save-display", "not valid!"], "{}")
check("conv-save-display refuses a bad id", rc != 0 and "invalid" in out, (rc, out))
rc, out, _ = _cli(["conv-save-display", cid], "[1,2]")
check("conv-save-display refuses a payload that is not an object", rc != 0 and "malformed" in out, (rc, out))

# ------------------------------------------------------------ 6. reserved names
check("both new verbs are reserved command names",
      "console-append-trace" in reserved_names.RESERVED_NAMES and "conv-save-display" in reserved_names.RESERVED_NAMES)
check("'trace' is a console_store kind", "trace" in console_store.KINDS)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
