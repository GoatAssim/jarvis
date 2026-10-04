"""Who used how much -- the per-person usage ledger behind Channels > Usage (L.36-P2).

WHY A LEDGER OF ITS OWN
-----------------------
Nothing that already exists records who a request was FOR. `logs.py` writes
one usage row per provider round into the conversation's file, and a chat
thread is one conversation, so a DM thread could be attributed to its one
person after the fact -- but a group thread mixes people and its rows say
nothing about which of them asked. The only place that knows both the sender
and the finished ask is `base.handle_message`, so it writes one small line
here when an ask ends:

    ~/.jarvis/channels/usage.jsonl     append-only, one JSON object per ask

WHAT IS IN A LINE -- AND WHAT IS NOT
------------------------------------
Counts and tool NAMES only: platform, user id, thread id, when, which
provider answered, input/output/thinking/total tokens, how many provider
requests, how many tool calls and which tools. Never the message, the reply,
a tool's arguments or its result, so this file can be shown, kept or deleted
without being a second copy of anyone's conversation. It is written whether or
not `log_conversations` is on, for that reason.

TOKENS, NOT MONEY
-----------------
The repo has no price table (providers differ, and several keys are free
tiers), so this reports tokens as the provider counted them and never invents
a dollar figure. Tool-call tokens in `logs.py` are ESTIMATES (~4 chars per
token); the totals here come from the ask's own ledger (`AskResult.usage_total`)
and are the providers' real counts across every attempt, key and round.

HISTORY BEGINS WHEN THIS FILE DOES
----------------------------------
An ask made before the ledger existed was never attributed to anyone, and this
module does not guess. The summary says when tracking began. The MESSAGE
counts (sent, turned away, answered) come from the transcript, which has
always recorded them, so those cover the whole history.

Rotated like the transcript (kept, never deleted). Every function here that
writes never raises: losing a usage line must never be the reason a message
goes unanswered.
"""

import json
import re
from datetime import datetime, timedelta

from . import transcript
from .directory import CHANNELS_DIR

ENCODING = "utf-8"
LEDGER = CHANNELS_DIR / "usage.jsonl"
MAX_BYTES = 2_000_000
MAX_DAYS = 90
_ROTATED = re.compile(r"^usage\.\d{8}-\d{6}\.jsonl$")
TOP_N = 8


def _int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _now():
    return datetime.now().replace(microsecond=0)


def _rotate_if_needed():
    try:
        if LEDGER.exists() and LEDGER.stat().st_size > MAX_BYTES:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            LEDGER.rename(LEDGER.with_name(f"usage.{stamp}.jsonl"))
    except OSError:
        pass  # a failed rotate must not stop the append


def tools_from_result(result):
    """Tool names an AskResult called, in order, [] when none were tracked.
    `AskResult.usage["tool_calls"]` covers the attempt that answered; calls
    made on an attempt that then failed over are not in it."""
    usage = getattr(result, "usage", None)
    calls = usage.get("tool_calls") if isinstance(usage, dict) else None
    names = []
    for call in calls or []:
        name = call.get("name") if isinstance(call, dict) else None
        if isinstance(name, str) and name:
            names.append(name[:64])
    return names


def record(platform, user_id, thread_id="", conv_id=None, provider="", ok=True,
           usage_total=None, tool_names=None, tools_mode="", error=""):
    """Append one finished ask. Returns True if it was written; never raises."""
    try:
        total = usage_total if isinstance(usage_total, dict) else {}
        names = [str(n) for n in (tool_names or [])]
        entry = {
            "at": _now().isoformat(),
            "platform": str(platform),
            "user_id": str(user_id or ""),
            "thread_id": str(thread_id or ""),
            "conv_id": conv_id or None,
            "provider": str(provider or ""),
            "ok": bool(ok),
            "input_tokens": _int(total.get("input_tokens")),
            "output_tokens": _int(total.get("output_tokens")),
            "thinking_tokens": _int(total.get("thinking_tokens")),
            "total_tokens": _int(total.get("total_tokens")),
            "requests": _int(total.get("requests")),
            "tool_calls": len(names),
            "tools": names,
            "tools_mode": str(tools_mode or ""),
        }
        if error:
            entry["error"] = str(error)[:200]
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        with LEDGER.open("a", encoding=ENCODING) as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def _ledger_files():
    """Rotated copies oldest first, live file last."""
    folder = LEDGER.parent
    if not folder.is_dir():
        return []
    rotated = sorted(p for p in folder.glob("usage.*.jsonl")
                     if _ROTATED.match(p.name))
    return rotated + ([LEDGER] if LEDGER.exists() else [])


def _read_ledger():
    out = []
    for path in _ledger_files():
        try:
            text = path.read_text(encoding=ENCODING)
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
    return out


def _when(text):
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def _fold(entries):
    """Totals over a list of ledger entries."""
    totals = {"asks": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0,
              "thinking_tokens": 0, "total_tokens": 0, "requests": 0,
              "tool_calls": 0}
    tools, providers = {}, {}
    for e in entries:
        totals["asks"] += 1
        if e.get("ok") is False:
            totals["failed"] += 1
        for key in ("input_tokens", "output_tokens", "thinking_tokens",
                    "total_tokens", "requests", "tool_calls"):
            totals[key] += _int(e.get(key))
        for name in e.get("tools") or []:
            if isinstance(name, str):
                tools[name] = tools.get(name, 0) + 1
        provider = e.get("provider") or "(none)"
        slot = providers.setdefault(provider, {"provider": provider, "asks": 0,
                                               "total_tokens": 0})
        slot["asks"] += 1
        slot["total_tokens"] += _int(e.get("total_tokens"))
    ranked = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
    top_providers = sorted(providers.values(),
                           key=lambda p: (-p["total_tokens"], p["provider"]))[:TOP_N]
    totals["avg_tokens_per_ask"] = (round(totals["total_tokens"] / totals["asks"])
                                    if totals["asks"] else 0)
    return totals, [{"tool": k, "calls": v} for k, v in ranked], top_providers


def _message_counts(platform, user_id, since):
    """Sent / turned away / answered, from the transcript (whole history, so
    it does not depend on this ledger existing)."""
    matched, _ = transcript.person_records(platform, user_id)
    out = {"sent": 0, "turned_away": 0, "replies": 0,
           "sent_all_time": 0, "first_message": ""}
    for record, _via, _thread in matched:
        if record.get("dir") == "in":
            out["sent_all_time"] += 1
            if not out["first_message"]:
                out["first_message"] = record.get("at") or ""
        when = _when(record.get("at"))
        if since is not None and (when is None or when < since):
            continue
        if record.get("dir") == "in":
            out["sent"] += 1
            if record.get("allowed") is False:
                out["turned_away"] += 1
        elif record.get("dir") == "out" and record.get("ok") is not False:
            out["replies"] += 1
    return out


def summary(platform, user_id, days=30, now=None):
    """One person's usage over the last `days` days (1..MAX_DAYS), plus the
    all-time totals. Read-only."""
    uid = str(user_id or "").strip()
    try:
        days = max(1, min(int(days), MAX_DAYS))
    except (TypeError, ValueError):
        days = 30
    now = now or _now()
    since = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    entries = _read_ledger()
    on_platform = [e for e in entries if e.get("platform") == platform]
    mine = [e for e in on_platform if str(e.get("user_id") or "") == uid]
    mine_window = [e for e in mine if (_when(e.get("at")) or datetime.min) >= since]
    platform_window = [e for e in on_platform
                       if (_when(e.get("at")) or datetime.min) >= since]

    totals, tools, providers = _fold(mine_window)
    all_time, _, _ = _fold(mine)
    platform_tokens = sum(_int(e.get("total_tokens")) for e in platform_window)

    series = []
    for offset in range(days):
        day = (since + timedelta(days=offset)).date()
        series.append({"date": day.isoformat(), "asks": 0, "total_tokens": 0})
    index = {row["date"]: row for row in series}
    for e in mine_window:
        when = _when(e.get("at"))
        row = index.get(when.date().isoformat()) if when else None
        if row:
            row["asks"] += 1
            row["total_tokens"] += _int(e.get("total_tokens"))

    started = min((e.get("at") for e in entries if e.get("at")), default="")
    return {
        "ok": True,
        "platform": platform,
        "user_id": uid,
        "days": days,
        "since": since.date().isoformat(),
        "window": totals,
        "all_time": all_time,
        "tools": tools,
        "providers": providers,
        "series": series,
        "messages": _message_counts(platform, uid, since),
        # What share of everything this platform spent in the window was theirs.
        "platform_total_tokens": platform_tokens,
        "share_of_platform": (round(100 * totals["total_tokens"] / platform_tokens, 1)
                              if platform_tokens else None),
        # None = nothing has been recorded by anyone yet.
        "tracking_since": started or None,
        "has_ledger": bool(entries),
    }
