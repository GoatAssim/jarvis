#!/usr/bin/env python3
"""Measure one run from its event log (N32, brief 8D).  Not a test: `tests/test_play_trace_stats.py` tests it.

    python tests/play_trace_stats.py <event-log.jsonl>

Run it on a log before a change and on a log after it, and compare the two outputs.  It prints:

  * the number of rounds (one per model request) and the whole turn's time;
  * each tool call in order: the round, the tool's name, its time and the size of its result in characters;
  * per request: its size, split into FIXED (the system prompt plus the tool schemas, sent again every round)
    and the rest (the conversation so far), with the model's time for that round and the input tokens it reported;
  * the totals from the `ask_usage` row.

How things are counted (the same way finding 04 counted them by hand):

  * Sizes are characters of `json.dumps(...)` with Python's default separators.  A request is its whole payload.
    FIXED is the system instruction as the payload holds it plus each tool declaration on its own (the commas that
    join the declarations are not counted).  The Gemini shape was checked against a real log; the Anthropic and
    OpenAI shapes are read the same way but were only tried on hand-made rows.
  * A request row that was cut short (`truncated`) has no known size: it is listed as "cut short" with the number of
    characters that survived, and its fixed part is not guessed.
  * A tool's time is the gap between the moment the model answered (the `response` row, or the previous tool's
    result row when one answer called several tools) and the tool's own row.  THIS IS AN ASSUMPTION: it is right if a
    tool's row is written when the tool returns.  The log has no start stamp.
  * A model round's time is the gap between its `request` row and its `response` row.

It needs no network, no key and nothing outside the standard library.  It never prints a tool's arguments or result
text, a URL, or the log's path; it prints only numbers and tool names, and every printed line is also scrubbed of
anything that looks like a magnet link, a hash or a file path, in case a name field ever held one.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

SYSTEM_KEYS = ("systemInstruction", "system_instruction", "system", "instructions")
DECLARATION_KEYS = ("function_declarations", "functionDeclarations")

_SCRUBS = (
    (re.compile(r"magnet:\?\S*", re.IGNORECASE), "[hidden]"),
    (re.compile(r"\b[0-9a-fA-F]{32,64}\b"), "[hidden]"),
    (re.compile(r"[A-Za-z]:[\\/][^\s]*"), "[hidden]"),
    (re.compile(r"\\\\[^\s]+"), "[hidden]"),
    (re.compile(r"(?<![\w.])/(?:home|Users|mnt|tmp|var|etc|root|opt|usr|media|Volumes)/\S*"), "[hidden]"),
)


def scrub(text):
    """Hide anything that looks like a magnet link, a hash or a file path."""
    text = str(text)
    for pattern, replacement in _SCRUBS:
        text = pattern.sub(replacement, text)
    return text


_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,39}")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


def safe_name(value):
    """A tool name that is safe to print: it must look like a plain identifier (stream_search), otherwise it is "?".
    A magnet, a hash or a path in a name field is therefore never printed, not even in part."""
    text = str(value if value is not None else "")
    return scrub(text) if _TOOL_NAME.fullmatch(text) else "?"


def safe_file(value):
    """The log's file name (never its folder) if it is plain letters, digits and _ . -, otherwise "?"."""
    text = str(value if value is not None else "")
    return scrub(text) if _FILE_NAME.fullmatch(text) else "?"


def dumps_len(value):
    """The size of a value as `json.dumps` prints it (the measure finding 04 used)."""
    try:
        return len(json.dumps(value))
    except (TypeError, ValueError):
        return len(str(value))


# --------------------------------------------------------------------------- reading the log

def read_rows(path):
    """-> (rows, unreadable line numbers).  A row is a JSON object on one line; anything else is "unreadable"."""
    rows, bad = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad.append(number)
                continue
            if not isinstance(row, dict):
                bad.append(number)
                continue
            rows.append(row)
    return rows, bad


def _seconds(row):
    """A row's `ts` as seconds since the epoch, or None."""
    text = row.get("ts") if isinstance(row, dict) else None
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _data(row):
    data = row.get("data")
    return data if isinstance(data, dict) else {}


def split_turns(rows):
    """A turn ends at a row that carries `ask_usage`.  -> [rows of turn 1, rows of turn 2, ...]; a tail without one is a turn too."""
    turns, current = [], []
    for row in rows:
        current.append(row)
        if isinstance(_data(row).get("ask_usage"), dict):
            turns.append(current)
            current = []
    if any(r.get("direction") in ("request", "tool_call") for r in current):
        turns.append(current)
    return turns


# --------------------------------------------------------------------------- one request

def split_request(payload):
    """A request's payload -> {"total", "system", "tools", "n_tools", "fixed", "rest"}, or None when its shape is not recognised.

    fixed = the system instruction (as the payload holds it) + each tool declaration on its own."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    total = dumps_len(payload)
    system, found = 0, False
    for key in SYSTEM_KEYS:
        if key in payload:
            system += dumps_len(payload[key])
            found = True
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") in ("system", "developer"):
                system += dumps_len(message)
                found = True
    tools_chars, n_tools = 0, 0
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        found = True
        for item in tools:
            declarations = None
            if isinstance(item, dict):
                for key in DECLARATION_KEYS:
                    if isinstance(item.get(key), list):
                        declarations = item[key]
            if declarations is not None:
                for declaration in declarations:
                    tools_chars += dumps_len(declaration)
                    n_tools += 1
            else:
                tools_chars += dumps_len(item)
                n_tools += 1
    if not found:
        return None
    fixed = system + tools_chars
    return {"total": total, "system": system, "tools": tools_chars, "n_tools": n_tools, "fixed": fixed,
            "rest": max(0, total - fixed)}


def _request_entry(row, number):
    data = _data(row)
    entry = {"n": number, "truncated": False, "chars": None, "shown_chars": None, "system": None, "tools": None,
             "n_tools": None, "fixed": None, "rest": None, "fixed_pct": None, "model_s": None, "input_tokens": None}
    if data.get("truncated"):
        entry["truncated"] = True
        preview = data.get("preview")
        entry["shown_chars"] = len(preview) if isinstance(preview, str) else None
        return entry
    parts = split_request(data.get("payload"))
    if parts is None:
        payload = data.get("payload")
        entry["chars"] = dumps_len(payload) if payload is not None else None
        return entry
    entry.update(chars=parts["total"], system=parts["system"], tools=parts["tools"], n_tools=parts["n_tools"],
                 fixed=parts["fixed"], rest=parts["rest"])
    if parts["total"]:
        entry["fixed_pct"] = round(100.0 * parts["fixed"] / parts["total"], 1)
    return entry


# --------------------------------------------------------------------------- one turn

def analyze(rows, turn=1):
    """The rows of ONE turn -> a dict of numbers (see the module text).  Never raises on odd rows."""
    out = {"turn": turn, "rows": len(rows), "finished": False, "seconds_total": None, "rounds": 0, "usage_rows": 0,
           "model_s_total": 0.0, "tool_s_total": 0.0, "requests": [], "calls": [], "ask_usage": None, "notes": []}
    stamps = [s for s in (_seconds(r) for r in rows) if s is not None]
    if len(stamps) >= 2:
        out["seconds_total"] = round(max(stamps) - min(stamps), 3)
    last_request_ts = last_response_ts = last_result_ts = None
    pending = None                                   # a tool_call row waiting for its tool_result row
    usage = []
    for row in rows:
        kind, ts, data = row.get("direction"), _seconds(row), _data(row)
        if kind == "request":
            out["requests"].append(_request_entry(row, len(out["requests"]) + 1))
            last_request_ts = ts
        elif kind == "response":
            if ts is not None and last_request_ts is not None and out["requests"]:
                gap = round(ts - last_request_ts, 3)
                if out["requests"][-1]["model_s"] is None:
                    out["requests"][-1]["model_s"] = gap
                    out["model_s_total"] += gap
            last_response_ts = ts
        elif kind == "usage":
            usage.append(data)
        elif kind == "tool_call":
            if pending is not None:
                out["calls"].append(pending)
            starts = [t for t in (last_response_ts, last_result_ts) if t is not None]
            seconds = round(ts - max(starts), 3) if (ts is not None and starts) else None
            pending = {"n": len(out["calls"]) + 1, "round": len(out["requests"]), "name": safe_name(data.get("name")),
                       "seconds": seconds, "result_chars": None, "ts": ts}
            if seconds is not None:
                out["tool_s_total"] += seconds
        elif kind == "tool_result":
            last_result_ts = ts if ts is not None else last_result_ts
            if pending is not None:
                if "result" in data:
                    pending["result_chars"] = dumps_len(data.get("result"))
                out["calls"].append(pending)
                pending = None
        if isinstance(data.get("ask_usage"), dict):
            out["ask_usage"] = data["ask_usage"]
            out["finished"] = True
    if pending is not None:
        out["calls"].append(pending)
    for call in out["calls"]:
        call.pop("ts", None)
    out["rounds"] = len(out["requests"])
    out["usage_rows"] = len(usage)
    for index, entry in enumerate(out["requests"]):
        if index < len(usage):
            tokens = usage[index].get("input_tokens")
            entry["input_tokens"] = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None
    out["model_s_total"] = round(out["model_s_total"], 3)
    out["tool_s_total"] = round(out["tool_s_total"], 3)
    if out["usage_rows"] != out["rounds"]:
        out["notes"].append("%d request rows but %d usage rows: a request may have failed or been retried" % (out["rounds"], out["usage_rows"]))
    reported = out["ask_usage"].get("rounds") if isinstance(out["ask_usage"], dict) else None
    if isinstance(reported, int) and reported != out["rounds"]:
        out["notes"].append("ask_usage says %d rounds, the request rows say %d" % (reported, out["rounds"]))
    if not out["finished"]:
        out["notes"].append("no ask_usage row: the turn may not have finished, or the log is cut")
    cut = [e["n"] for e in out["requests"] if e["truncated"]]
    if cut:
        out["notes"].append("request %s: the log row was cut short, so its size and fixed part are not known" % ", ".join(str(n) for n in cut))
    unknown = [e["n"] for e in out["requests"] if not e["truncated"] and e["fixed"] is None]
    if unknown:
        out["notes"].append("request %s: payload shape not recognised, so its fixed part is not known" % ", ".join(str(n) for n in unknown))
    return out


# --------------------------------------------------------------------------- the text

def _num(value):
    return "{:,}".format(value) if isinstance(value, int) and not isinstance(value, bool) else "-"


def _sec(value):
    return "%.1f s" % value if isinstance(value, (int, float)) else "-"


def format_report(stats, source_name="", turns=1):
    """One turn's numbers -> the text the owner reads (a list of lines, already scrubbed)."""
    lines = []
    title = "Play trace stats" + (": " + safe_file(source_name) if source_name else "")
    if turns > 1:
        title += " (turn %d of %d)" % (stats["turn"], turns)
    lines.append(title)
    lines.append("rows read: %d" % stats["rows"])
    lines.append("whole turn: %s (first row to last row)" % _sec(stats["seconds_total"]))
    lines.append("rounds: %d (one per model request); usage rows: %d" % (stats["rounds"], stats["usage_rows"]))
    lines.append("model time (request row to response row): %s" % _sec(stats["model_s_total"]))
    lines.append("tool time: %s  [ASSUMPTION: a tool's row is written when the tool returns; the time is the gap from the "
                 "model's answer, or the previous tool's result, to the tool's row]" % _sec(stats["tool_s_total"]))
    lines.append("")
    lines.append("tool calls, in order:")
    if stats["calls"]:
        lines.append("  %-3s %-5s %-26s %10s %14s" % ("#", "round", "tool", "time", "result chars"))
        for call in stats["calls"]:
            lines.append("  %-3d %-5d %-26s %10s %14s" % (call["n"], call["round"], call["name"], _sec(call["seconds"]), _num(call["result_chars"])))
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("requests (characters of the payload as json.dumps prints it; fixed = system prompt + tool schemas):")
    if stats["requests"]:
        lines.append("  %-5s %9s %9s %7s %9s %9s %12s" % ("round", "total", "fixed", "fixed%", "rest", "model", "input tokens"))
        for e in stats["requests"]:
            if e["truncated"]:
                shown = "at least %s shown" % _num(e["shown_chars"]) if e["shown_chars"] is not None else "size unknown"
                lines.append("  %-5d cut short in the log (%s); size and fixed part not known   %9s %12s"
                             % (e["n"], shown, _sec(e["model_s"]), _num(e["input_tokens"])))
                continue
            pct = "%.1f%%" % e["fixed_pct"] if e["fixed_pct"] is not None else "-"
            lines.append("  %-5d %9s %9s %7s %9s %9s %12s" % (e["n"], _num(e["chars"]), _num(e["fixed"]), pct, _num(e["rest"]),
                                                              _sec(e["model_s"]), _num(e["input_tokens"])))
        first = next((e for e in stats["requests"] if e["fixed"] is not None), None)
        if first is not None:
            lines.append("  fixed part of request %d: system %s + %s tool schemas %s" % (first["n"], _num(first["system"]),
                                                                                       _num(first["n_tools"]), _num(first["tools"])))
    else:
        lines.append("  (none)")
    lines.append("")
    usage = stats["ask_usage"]
    if isinstance(usage, dict):
        keys = ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "rounds", "attempt_count")
        lines.append("totals from the ask_usage row: " + ", ".join("%s %s" % (k, _num(usage.get(k))) for k in keys if k in usage))
        attempts = usage.get("attempts")
        if isinstance(attempts, list):
            cached = [a.get("cache_read_tokens") for a in attempts if isinstance(a, dict) and isinstance(a.get("cache_read_tokens"), int)]
            if cached:
                lines.append("cache_read_tokens (all attempts): %s" % _num(sum(cached)))
    else:
        lines.append("totals from the ask_usage row: none in this log")
    for note in stats["notes"]:
        lines.append("note: " + note)
    return [scrub(line) for line in lines]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print("usage: python tests/play_trace_stats.py <event-log.jsonl>")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    path = argv[0]
    name = os.path.basename(path)
    try:
        rows, bad = read_rows(path)
    except OSError as exc:
        print("cannot read %s: %s" % (safe_file(name), type(exc).__name__))
        return 2
    if not rows:
        print("no readable rows in %s (%d unreadable lines)" % (safe_file(name), len(bad)))
        return 1
    turns = split_turns(rows) or [rows]
    for index, turn_rows in enumerate(turns, 1):
        if index > 1:
            print("")
        for line in format_report(analyze(turn_rows, index), name, len(turns)):
            print(line)
    if bad:
        print("note: %d unreadable lines skipped (line numbers: %s)" % (len(bad), ", ".join(str(n) for n in bad[:10])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
