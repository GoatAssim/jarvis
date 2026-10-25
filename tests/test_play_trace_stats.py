"""tests/play_trace_stats.py (N32, brief 8D) against the owner's censored event log of finding 04
(tests/data/play-flow-175.jsonl: one chat turn, 32 rows) and against small hand-made logs.  No network.

What is pinned from finding 04: 6 rounds; the five tool calls in order; request sizes 9,685 / 13,267 / 18,368 for
requests 1 to 3; the 96 % fixed share (9,325 of 9,685); 24,974 input tokens; the call times 9.6 / 15.5 / 0.0 / 20.6 / 0.4 s;
the 66.7 s turn.  Row 17 (counting from 0; line 18 of the file) is a request row the logger cut short.
The shapes of Anthropic and OpenAI requests are only tried on hand-made rows here, not on a real log.

Run: python tests/test_play_trace_stats.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import play_trace_stats as pts  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


def near(value, expected, tolerance):
    return isinstance(value, (int, float)) and abs(value - expected) <= tolerance


LOG = ROOT / "tests" / "data" / "play-flow-175.jsonl"
TMP = Path(tempfile.mkdtemp(prefix="jarvis-trace-stats-"))

# ---- where the log lives, and what is in it
check("the log is in tests/data, not tests/fixtures (test_replay_fixtures reads that folder in pairs)",
      LOG.is_file() and not (ROOT / "tests" / "fixtures" / "play-flow-175.jsonl").exists())
raw = LOG.read_text(encoding="utf-8")
check("the log holds no magnet link, 32-or-more-character hash or local path (N32b)",
      not re.search(r"magnet:\?", raw, re.IGNORECASE) and not re.search(r"\b[0-9a-fA-F]{32,64}\b", raw)
      and not re.search(r"[A-Za-z]:\\\\", raw) and not re.search(r"/(?:home|Users|mnt)/", raw))

rows, bad = pts.read_rows(str(LOG))
check("32 rows are read and none is unreadable", len(rows) == 32 and bad == [])
check("row 17 (from 0) is the request row that was cut short; the other five requests are whole",
      rows[17]["direction"] == "request" and rows[17]["data"].get("truncated") is True
      and sum(1 for r in rows if r["direction"] == "request" and not r["data"].get("truncated")) == 5)
turns = pts.split_turns(rows)
check("the log is one turn", len(turns) == 1 and len(turns[0]) == 32)
s = pts.analyze(turns[0])

# ---- finding 04's numbers
check("6 rounds, 6 usage rows", s["rounds"] == 6 and s["usage_rows"] == 6)
check("the five calls in order", [c["name"] for c in s["calls"]] == ["media_find", "stream_search", "play_episode", "play_episode", "stack_status"])
check("their rounds are 1 to 5", [c["round"] for c in s["calls"]] == [1, 2, 3, 4, 5])
times = [c["seconds"] for c in s["calls"]]
check("tool times: about 9.6, 15.5, 0.0, 20.6 and 0.4 s",
      near(times[0], 9.6, 0.05) and near(times[1], 15.5, 0.05) and near(times[2], 0.0, 0.05) and near(times[3], 20.6, 0.05) and near(times[4], 0.4, 0.05))
check("tool time in all: about 46 s", near(s["tool_s_total"], 46.1, 0.2))
check("model time in all: about 20 s", near(s["model_s_total"], 20.4, 0.2))
check("the whole turn: about 66.7 s", near(s["seconds_total"], 66.7, 0.1))
check("result sizes in characters, as json.dumps prints them (the stream_search one is 3,314; finding 04's '3.4 thousand' counted the whole row)",
      [c["result_chars"] for c in s["calls"]] == [1178, 3314, 94, 247, 693])
req = s["requests"]
check("request sizes 1 to 3: 9,685 / 13,267 / 18,368", [r["chars"] for r in req[:3]] == [9685, 13267, 18368])
check("requests 5 and 6 are 18,763 and 13,673 characters", req[4]["chars"] == 18763 and req[5]["chars"] == 13673)
check("request 1: 9,325 fixed = system 2,875 + 19 tool schemas 6,450; the rest is 360",
      req[0]["fixed"] == 9325 and req[0]["system"] == 2875 and req[0]["tools"] == 6450 and req[0]["n_tools"] == 19 and req[0]["rest"] == 360)
check("request 1: the fixed share is 96 % (96.3)", req[0]["fixed_pct"] == 96.3)
check("every whole request has the same fixed part", len({r["fixed"] for r in req if not r["truncated"]}) == 1)
check("request 4 is reported as cut short: no size, no fixed part, but the characters that survived",
      req[3]["truncated"] and req[3]["chars"] is None and req[3]["fixed"] is None and req[3]["shown_chars"] == 19563)
check("input tokens add up to 24,974 and request 1 reported 2,523", sum(r["input_tokens"] for r in req) == 24974 and req[0]["input_tokens"] == 2523)
u = s["ask_usage"]
check("ask_usage totals: 24,974 input, 406 output, 1,515 thinking, 26,895 in all, 6 rounds",
      u["input_tokens"] == 24974 and u["output_tokens"] == 406 and u["thinking_tokens"] == 1515 and u["total_tokens"] == 26895 and u["rounds"] == 6)
check("the turn is finished and the only note is the cut-short request",
      s["finished"] and len(s["notes"]) == 1 and "request 4" in s["notes"][0] and "cut short" in s["notes"][0])

# ---- the text
text = "\n".join(pts.format_report(s, "play-flow-175.jsonl", 1))
for needle in ("9,685", "13,267", "18,368", "24,974", "96.3%", "cut short", "ASSUMPTION", "rounds: 6", "stream_search", "3,314", "2,875", "6,450"):
    check("the report text says %r" % needle, needle in text)
check("the report text prints no URL, key label, argument or magnet",
      "http" not in text and "generativelanguage" not in text and "key 1/10" not in text and "rick" not in text.lower() and "magnet" not in text.lower())

# ---- the command line
path_arg = str(LOG)
run = subprocess.run([sys.executable, str(ROOT / "tests" / "play_trace_stats.py"), path_arg], capture_output=True, text=True, timeout=60)
check("the command line exits 0 and prints the numbers", run.returncode == 0 and "9,685" in run.stdout and "rounds: 6" in run.stdout)
check("the command line never prints the log's folder", str(LOG.parent) not in run.stdout and "tests/data" not in run.stdout)
run = subprocess.run([sys.executable, str(ROOT / "tests" / "play_trace_stats.py"), str(TMP / "missing.jsonl")], capture_output=True, text=True, timeout=60)
check("a missing file exits 2 with one line and no folder", run.returncode == 2 and str(TMP) not in run.stdout + run.stderr and "Traceback" not in run.stderr)
empty = TMP / "empty.jsonl"
empty.write_text("", encoding="utf-8")
run = subprocess.run([sys.executable, str(ROOT / "tests" / "play_trace_stats.py"), str(empty)], capture_output=True, text=True, timeout=60)
check("an empty log exits 1", run.returncode == 1 and "no readable rows" in run.stdout)
run = subprocess.run([sys.executable, str(ROOT / "tests" / "play_trace_stats.py")], capture_output=True, text=True, timeout=60)
check("no argument prints the usage and exits 2", run.returncode == 2 and "usage" in run.stdout)

# ---- odd logs
mixed = TMP / "mixed.jsonl"
mixed.write_text(raw + "this is not json\n[1, 2]\n\n", encoding="utf-8")
rows2, bad2 = pts.read_rows(str(mixed))
check("lines that are not JSON objects are counted as unreadable, blank lines are not", len(rows2) == 32 and len(bad2) == 2)
run = subprocess.run([sys.executable, str(ROOT / "tests" / "play_trace_stats.py"), str(mixed)], capture_output=True, text=True, timeout=60)
check("the command line says how many lines it skipped", run.returncode == 0 and "2 unreadable lines skipped" in run.stdout)

two = pts.split_turns(rows + rows)
check("two turns in one log are split at the ask_usage row and both give 6 rounds",
      len(two) == 2 and [pts.analyze(t, i)["rounds"] for i, t in enumerate(two, 1)] == [6, 6])
cut = pts.analyze(rows[:31])
check("a log cut before the ask_usage row is not 'finished' and says so", not cut["finished"] and cut["ask_usage"] is None and any("no ask_usage" in n for n in cut["notes"]))
check("a log with only info rows has no turns, and analyze still does not raise", pts.split_turns(rows[:1]) == []
      and pts.analyze([])["rounds"] == 0)
no_ts = [dict(r, ts="not a time") for r in rows]
a = pts.analyze(no_ts)
check("rows with unreadable stamps give no times, only sizes", a["seconds_total"] is None and a["calls"][0]["seconds"] is None and a["requests"][0]["chars"] == 9685)

# ---- several tools after one answer: the second is timed from the first one's result
def row(direction, ts, data):
    return {"ts": "2026-01-01T00:00:%06.3f+00:00" % ts, "direction": direction, "provider": "x", "round": None, "data": data}


par = [row("request", 0.0, {"payload": {"contents": []}}), row("response", 1.0, {"status": 200}),
       row("tool_call", 3.0, {"name": "a_tool"}), row("tool_result", 3.0, {"name": "a_tool", "result": {"ok": True}}),
       row("tool_call", 7.0, {"name": "b_tool"}), row("tool_result", 7.0, {"name": "b_tool", "result": "plain text"}),
       row("info", 8.0, {"ask_usage": {"input_tokens": 5, "rounds": 1}})]
p = pts.analyze(par)
check("two tools after one answer: the first is timed from the answer (2 s), the second from the first's result (4 s)",
      [c["seconds"] for c in p["calls"]] == [2.0, 4.0] and [c["round"] for c in p["calls"]] == [1, 1])
check("a result that is a string is counted as json.dumps prints it", p["calls"][1]["result_chars"] == len(json.dumps("plain text")))
check("a request whose payload has no system or tools says its fixed part is unknown, not 0",
      p["requests"][0]["fixed"] is None and any("shape not recognised" in n for n in p["notes"]))

# ---- other request shapes (hand-made)
anth = {"model": "m", "system": "S" * 100, "tools": [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}],
        "messages": [{"role": "user", "content": "hi"}]}
sp = pts.split_request(anth)
check("an Anthropic-style payload: the system string and each tool are fixed",
      sp["system"] == len(json.dumps("S" * 100)) and sp["n_tools"] == 2 and sp["fixed"] == sp["system"] + sp["tools"] and sp["total"] == len(json.dumps(anth)))
oai = {"model": "m", "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
       "tools": [{"type": "function", "function": {"name": "a"}}]}
sp = pts.split_request(oai)
check("an OpenAI-style payload: the system message and the tool are fixed, the user message is not",
      sp["system"] == len(json.dumps(oai["messages"][0])) and sp["n_tools"] == 1 and sp["rest"] == sp["total"] - sp["fixed"])
check("a payload given as a JSON string is read", pts.split_request(json.dumps(anth))["n_tools"] == 2)
check("something that is not a payload gives None", pts.split_request("not json") is None and pts.split_request(5) is None and pts.split_request({"contents": []}) is None)

# ---- never prints a magnet, a hash or a path
HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = "magnet:?xt=urn:btih:" + HASH + "&dn=Secret.Show.S01E03"
WINPATH = "C:\\Users\\owner\\Videos\\Secret.Show.S01E03.mkv"
UNIXPATH = "/home/owner/Videos/Secret.Show.S01E03.mkv"
evil = [row("request", 0.0, {"payload": {"systemInstruction": {"parts": [{"text": "sys " + MAGNET}]},
                                         "tools": [{"function_declarations": [{"name": MAGNET, "description": WINPATH}]}],
                                         "contents": [{"parts": [{"text": UNIXPATH}]}]}}),
        row("response", 1.0, {"status": 200, "body": {"x": MAGNET}}),
        row("tool_call", 2.0, {"name": MAGNET, "arguments": {"path": WINPATH, "hash": HASH}}),
        row("tool_result", 2.0, {"name": MAGNET, "result": {"magnet": MAGNET, "file": UNIXPATH}}),
        row("tool_call", 3.0, {"name": HASH, "arguments": {}}),
        row("tool_result", 3.0, {"name": HASH, "result": {}}),
        row("tool_call", 4.0, {"name": WINPATH, "arguments": {}}),
        row("tool_result", 4.0, {"name": WINPATH, "result": {}}),
        row("info", 5.0, {"ask_usage": {"input_tokens": 1, "rounds": 1}})]
log = TMP / ("%s.jsonl" % HASH)
log.write_text("\n".join(json.dumps(r) for r in evil) + "\n", encoding="utf-8")
run = subprocess.run([sys.executable, str(ROOT / "tests" / "play_trace_stats.py"), str(log)], capture_output=True, text=True, timeout=60)
out = run.stdout + run.stderr
check("a log full of magnets, hashes and paths still runs", run.returncode == 0 and "rounds: 1" in run.stdout)
check("none of them is printed, whole or in part: no magnet, no hash, no path, no file name that is a hash",
      "magnet" not in out.lower() and HASH not in out and HASH[:20] not in out and "Secret" not in out and "owner" not in out
      and "Users" not in out and "/home" not in out and str(TMP) not in out)
check("the names that are not plain identifiers print as '?'", "?" in out and out.count(" ? ") >= 3)
check("scrub() hides a magnet, a hash and both kinds of path inside any text",
      all(x not in pts.scrub("see " + x + " here") for x in (MAGNET, HASH, WINPATH, UNIXPATH)))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
