"""L.35 -- Log search "kinda messed up". Defects found in the code (no symptom
was ever given), each pinned here:

  log_files.search (the Log search panel)
    * a line was cut to 2000 characters BEFORE matching, so text deeper in a
      long log line could not be found;
    * lines were split with str.splitlines(), which also breaks on U+2028 etc.,
      so line numbers drifted and a JSON line was cut in two;
    * conversation logs are JSON, so a Windows path, a quote or a line break was
      stored escaped and a search for what the log SHOWS found nothing.
  logs.search (the Logs panel's deep search): the same escaping problem.
  CLI: text starting with "--" was read as a flag; `--query=<text>` carries it.

Run: python3 tests/test_l35_log_search_fixes.py
Touches no real ~/.jarvis (HOME and the log dir are redirected to a temp dir).
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import log_files, logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


TMP = Path(_HOME) / "files"
TMP.mkdir(parents=True, exist_ok=True)


def write(name, text):
    path = TMP / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


def find(query, path, **kw):
    return log_files.search(query, sets=[], paths=[str(path)], **kw)


# ---------------------------------------------------------------------------
# Long lines
# ---------------------------------------------------------------------------
long_line = "x" * 5000 + " NEEDLE_DEEP " + "y" * 5000
p = write("long.log", "first\n" + long_line + "\nlast\n")
out = find("NEEDLE_DEEP", p)
check("a match far past 2000 characters is found", out["ok"] and len(out["results"]) == 1, out)
hit = out["results"][0] if out["results"] else {"text": "", "line": 0}
check("...and the shown text is bounded but contains the match",
      len(hit["text"]) <= log_files.MAX_LINE_CHARS + 2 and "NEEDLE_DEEP" in hit["text"], len(hit["text"]))
check("...on the right line number", hit["line"] == 2, hit)

p = write("short.log", "plain short NEEDLE line\n")
check("a short line is shown exactly as before", find("needle", p)["results"][0]["text"] == "plain short NEEDLE line")

# ---------------------------------------------------------------------------
# Line splitting
# ---------------------------------------------------------------------------
p = write("sep.jsonl", '{"a": "one"}\n{"a": "two\u2028still two"}\n{"a": "TARGET three"}\n')
out = find("TARGET", p)
check("U+2028 inside a line no longer shifts later line numbers",
      out["results"] and out["results"][0]["line"] == 3, out["results"])
check("...and the line holding it is not cut in two",
      find("still", p)["results"][0]["line"] == 2)
p = write("crlf.log", "a\r\nb\r\nTARGET\r\n")
check("CRLF files still number correctly", find("TARGET", p)["results"][0]["line"] == 3)
p = write("trail.log", "a\nTARGET\n")
check("the file's trailing newline is not a phantom line", log_files._split_lines("a\nb\n") == ["a", "b"])

# ---------------------------------------------------------------------------
# JSON-escaped text
# ---------------------------------------------------------------------------
entry = {"ts": "2026-10-11", "direction": "tool_call",
         "data": {"cmd": r"C:\Users\me\app.exe --stream", "say": 'he said "hello there"',
                  "note": "line one\nline two"}}
p = write("conv.jsonl", json.dumps(entry, ensure_ascii=False) + "\n" + json.dumps({"data": {"cmd": "other"}}) + "\n")

out = find(r"C:\Users\me", p, mode="phrase")
check("a Windows path is found by the text the log shows", len(out["results"]) == 1, out)
check("...the hit says it matched the decoded text", out["results"] and out["results"][0].get("decoded") is True)
check("...and shows the readable path, not the doubled backslashes",
      out["results"] and r"C:\Users\me\app.exe" in out["results"][0]["text"], out["results"])
out = find('said "hello there"', p, mode="phrase")
check("a phrase containing quotes is found", len(out["results"]) == 1, out)
out = find("line one line two", p, mode="words")
check("words on separate lines of one value are found (words mode)", len(out["results"]) == 1, out)
out = find(r"Users.me.app", p, mode="regex")
check("regex over the decoded text works", len(out["results"]) == 1, out)
out = find("app.exe", p, mode="phrase")
check("text that matched the raw line is unchanged (not flagged decoded)",
      len(out["results"]) == 1 and not out["results"][0].get("decoded"), out)
check("a non-matching query still finds nothing", find("nothing here at all", p)["results"] == [])

check("_decode_line: no backslash means no parse", log_files._decode_line('{"a": "b"}') == "")
check("_decode_line: not JSON means no text", log_files._decode_line('plain \\ text') == "")
check("_decode_line: broken JSON means no text", log_files._decode_line('{"a": "b\\\\') == "")
check("_plain_text leaves keys out", "direction" not in log_files._plain_text(entry))
p = write("plain_backslash.log", r"C:\Users\me appears raw here" + "\n")
check("a non-JSONL file with backslashes still matches the raw text",
      len(find(r"C:\Users\me", p, mode="phrase")["results"]) == 1)

p = write("ctx.jsonl", "before\n" + json.dumps({"p": r"C:\a\b"}) + "\nafter\n")
out = find(r"C:\a\b", p, mode="phrase", context=1)
ctx = out["results"][0]["context"] if out["results"] else []
check("context for a decoded hit keeps its neighbours and shows the readable hit line",
      [c["text"] for c in ctx] == ["before", r"C:\a\b", "after"], ctx)

# ---------------------------------------------------------------------------
# logs.search (the Logs panel deep search)
# ---------------------------------------------------------------------------
logs.JARVIS_DIR = Path(_HOME) / ".jarvis"
logs.LOG_DIR = logs.JARVIS_DIR / "logs"
CONV = "a1b2c3d4e5f60718"
logs.log(CONV, "tool_call", {"cmd": r"C:\Users\me\app.exe", "text": "hello"})
res = logs.search(r"C:\Users\me", mode="phrase")
check("logs.search: a Windows path is found", res["ok"] and len(res["results"]) == 1, res)
check("logs.search: the snippet is readable text", res["results"] and r"C:\Users\me" in res["results"][0]["snippet"], res)
check("logs.search: a key name still matches, as before (additive change)",
      len(logs.search("cmd")["results"]) == 1)
check("logs.search: a value still matches", len(logs.search("hello")["results"]) == 1)
check("logs.search: no match still means no result", logs.search("zzz-not-there")["results"] == [])

# ---------------------------------------------------------------------------
# CLI: text that starts with "--"
# ---------------------------------------------------------------------------
from jarvis import channels_cli, workspace_cli  # noqa: E402

logs.log(CONV, "info", {"text": "launched with --stream flag"})
buf = io.StringIO()
code = None
with contextlib.redirect_stdout(buf):
    try:
        channels_cli.handle(["logs-search", "--query=--stream", "--mode", "phrase"])
    except SystemExit as exc:
        code = exc.code
out = json.loads(buf.getvalue())
check("logs-search --query=--stream exits 0 and searches for the flag text",
      code == 0 and out.get("query") == "--stream" and len(out["results"]) == 1, (code, out))

p = write("flags.log", "started with --stream on\nnothing\n")
buf = io.StringIO()
code = None
with contextlib.redirect_stdout(buf):
    try:
        workspace_cli.handle(["logs-files", "--query=--stream", "--path", str(p), "--set", "none"])
    except SystemExit as exc:
        code = exc.code
raw = buf.getvalue()
out = json.loads(raw) if raw.strip() else {}
check("logs-files --query=--stream searches for the flag text, not the flag",
      out.get("query") == "--stream" and any(r["line"] == 1 for r in out.get("results", [])), (code, out))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        workspace_cli.handle(["logs-files", "plain", "words", "--path", str(write("w.log", "plain words here\n"))])
    except SystemExit:
        pass
out = json.loads(buf.getvalue())
check("logs-files with positional words still works", out.get("query") == "plain words", out)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
