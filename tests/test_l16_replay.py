"""L.16 item 10: replay the real "check the screen, then shut down" incident
(`1a99e1e3f0d3af0e`, 2026-09-30) through the REAL ask() loop, offline, and
MEASURE how many model calls it takes -- instead of arguing it "by
construction".

What is real and what is not (read this before trusting a number):

  REAL      ai_client.ask(), the provider adapters, the round budget, the policy
            gate, the circuit breaker, the token ledger, tool_result_shaping,
            tool_diagnosis, and (scenario B) the real ocr_tools.tool_read_screen.
  RECORDED  every model reply for as long as the recording covers it: the
            original run's responses are served, per URL, in order (the F.17
            harness's rule, tests/test_replay_fixtures.py), usage included.
  SYNTHETIC The one request the original run never made: the closing "stop and
            report" call a stopped run makes with tools withheld. It gets a
            canned one-line answer, and its input tokens are ESTIMATED from the
            request's size at the chars-per-token ratio the recording itself
            shows (first request: 9,262 chars = 2,200 tokens). Every figure that
            depends on it is labelled "estimated".
  STUBBED   every tool except read_screen. system_tools.execute_tool is replaced,
            so this file can never click, type or shut anything down, on any OS.
            Stubbed tools return what the original run's tool_result rows say.

The recording has no `.json` conversation record (only the traffic log), so the
user text is taken from the first recorded request and the conversation starts
empty.

Scenarios:
  A  unattended, job NOT approved for the desktop  -> policy denies read_screen
  B  unattended, approved for the desktop, no OCR and no vision route
  C  CONTROL: the same recording with none of the L.16 guards (an interactive
     ask) -- it must reproduce the incident, or the replay proves nothing
  D  unattended, nothing stops it but the token budget (tool results as recorded)

Run: python3 tests/test_l16_replay.py
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import (ai_client, ai_config, ai_providers, conversations, key_health,  # noqa: E402
                    ocr_tools, token_usage)
from jarvis import tools as system_tools  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "1a99e1e3f0d3af0e.jsonl"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# ---------------------------------------------------------------------------
# Loading the recording
# ---------------------------------------------------------------------------
class _Resp:
    """Just enough of requests.Response for the adapters."""

    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = {}

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


_KEY_LABEL = re.compile(r"^(.*?)\s*\(key\s*(\d+)/(\d+)\)\s*$")


def _url_of(data):
    if data.get("url"):
        return data["url"]
    # The log truncates its larger request rows to a text preview; the URL is
    # still at the front of it.
    m = re.search(r'"url":\s*"([^"]+)"', data.get("preview") or "")
    return m.group(1) if m else ""


def load_recording():
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    queues, tool_results, buckets = {}, {}, {}
    pending = None
    user_text = None
    first_request_chars = None
    for row in rows:
        direction, data = row.get("direction"), row.get("data") or {}
        if direction == "request":
            url = _url_of(data)
            label = row.get("provider") or ""
            m = _KEY_LABEL.match(label)
            name, total = (m.group(1), int(m.group(3))) if m else (label, 1)
            payload = data.get("payload") or {}
            if name not in buckets:
                ptype = ("gemini" if "generativelanguage" in url else
                         "anthropic" if "anthropic.com" in url else "openai_compatible")
                model = payload.get("model") or (
                    re.search(r"/models/([^:/]+):", url).group(1) if ptype == "gemini" else "unknown")
                buckets[name] = {"url": url, "type": ptype, "model": model, "keys": total, "rank": len(buckets)}
            else:
                buckets[name]["keys"] = max(buckets[name]["keys"], total)
            if user_text is None and payload.get("contents"):
                user_text = payload["contents"][0]["parts"][0]["text"]
                first_request_chars = len(json.dumps(payload))
            pending = url
            queues.setdefault(url, [])
        elif direction in ("response", "error") and pending is not None:
            if direction == "response":
                queues[pending].append((data.get("status", 200), data.get("body", {})))
            else:
                queues[pending].append((None, data.get("error", "replayed network error")))
            pending = None
        elif direction == "tool_result":
            tool_results.setdefault(data.get("name"), []).append(data.get("result"))
    providers = []
    for name, b in sorted(buckets.items(), key=lambda kv: kv[1]["rank"]):
        providers.append({"name": name, "type": b["type"], "enabled": True, "base_url": b["url"],
                          "model": b["model"],
                          "api_keys": [f"replay-placeholder-{name}-{i}" for i in range(1, b["keys"] + 1)]})
    first_input_tokens = next(
        (r["data"]["input_tokens"] for r in rows if r.get("direction") == "usage"), None)
    return {
        "user_text": user_text, "queues": queues, "tool_results": tool_results, "providers": providers,
        "chars_per_token": first_request_chars / float(first_input_tokens),
    }


REC = load_recording()


def _synthetic_reply(url, est_input_tokens):
    text = "I couldn't check the screen, so nothing was typed and the PC was not shut down."
    if "generativelanguage" in url:
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": est_input_tokens, "candidatesTokenCount": 20}}
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": est_input_tokens, "completion_tokens": 20}}


# ---------------------------------------------------------------------------
# One replay
# ---------------------------------------------------------------------------
class Env:
    """Set/unset environment variables for the duration of one scenario."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def replay(env, real_read_screen):
    """Re-ask the incident's user text through ask() and return a dict of what
    the real loop did."""
    # A clean slate every time: the control scenario's 429s must not park keys
    # for the scenarios after it.
    try:
        key_health.HEALTH_FILE.unlink()
    except OSError:
        pass
    cfg = json.loads(json.dumps(ai_config.DEFAULT_AI_CONFIG))
    cfg["providers"] = json.loads(json.dumps(REC["providers"]))
    d = cfg["defaults"]
    d["provider_priority"] = [p["name"] for p in cfg["providers"]]
    d["min_round_interval_seconds"] = 0     # no real 3 s floor between rounds
    d["vision_enabled"] = False             # "no vision route", as in the incident's scenario B
    ai_config.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    ai_config.AI_CONFIG_FILE.write_text(json.dumps(cfg), encoding=ai_config.ENCODING)
    conv_id = conversations.new_conversation(make_current=False)

    cursor = {url: 0 for url in REC["queues"]}
    requests_made, served = [], []

    def fake_post_json(url, headers, payload, timeout):
        wants_tools = bool(payload.get("tools"))
        est = int(round(len(json.dumps(payload)) / REC["chars_per_token"]))
        requests_made.append({"url": url, "tools": wants_tools, "est_input": est})
        if not wants_tools:
            # The original run never made a tools-withheld call: synthetic.
            served.append("synthetic")
            return _Resp(200, _synthetic_reply(url, est)), None
        i = cursor.get(url, 0)
        seq = REC["queues"].get(url, [])
        if i >= len(seq):
            return None, "replay exhausted: no more recorded responses for " + url
        cursor[url] = i + 1
        status, body = seq[i]
        served.append("recorded")
        if status is None:
            return None, body
        return _Resp(status, body), None

    tool_cursor = {}
    ran, started = [], []

    def fake_execute_tool(name, arguments=None, verbosity=None, context=None):
        started.append(name)
        if name == "read_screen" and real_read_screen:
            return real_read_screen(arguments)
        recorded = REC["tool_results"].get(name) or []
        i = tool_cursor.get(name, 0)
        tool_cursor[name] = i + 1
        ran.append(name)
        return json.loads(json.dumps(recorded[i] if i < len(recorded) else {"ok": True}))

    names = []

    def on_tool_call(name, arguments):
        names.append(name)

    saved = (ai_providers._post_json, system_tools.execute_tool, time.sleep,
             ocr_tools.pytesseract, ocr_tools.Image)
    ai_providers._post_json = fake_post_json
    system_tools.execute_tool = fake_execute_tool
    time.sleep = lambda s: None                  # the 429's stated wait, and any pacing
    ocr_tools.pytesseract = None                 # Tesseract missing, whatever this machine has
    ocr_tools.Image = None
    try:
        with env:
            result = ai_client.ask(REC["user_text"], commands=[], conversation_id=conv_id,
                                   on_tool_call=on_tool_call,
                                   on_confirm_request=lambda n, a, risk_note=None: True)
    finally:
        (ai_providers._post_json, system_tools.execute_tool, time.sleep,
         ocr_tools.pytesseract, ocr_tools.Image) = saved
    return {"result": result, "requests": requests_made, "served": served,
            "tool_names": names, "tools_executed": ran, "tools_started": started}


def real_tokens(run):
    """Tokens of the RECORDED responses actually served (input + output +
    thinking), plus the estimated input of any synthetic call."""
    total_recorded, est_synthetic = 0, 0
    for req, kind in zip(run["requests"], run["served"]):
        if kind == "synthetic":
            est_synthetic += req["est_input"] + 20
    ledger = run["result"].usage_total or {}
    return ledger.get("total_tokens", 0), est_synthetic


def unattended(approved, budget=None):
    """A scheduled run: what the job was approved for, and an optional limit."""
    return {"JARVIS_SCHEDULED": "1", "JARVIS_CONTEXT": None,
            "JARVIS_JOB_APPROVED_KINDS": approved, "JARVIS_TOKEN_BUDGET": budget}

INTERACTIVE = {"JARVIS_SCHEDULED": None, "JARVIS_CONTEXT": None, "JARVIS_JOB_APPROVED_KINDS": None,
               "JARVIS_TOKEN_BUDGET": None}

report = []


def line(label, run):
    ledger = run["result"].usage_total or {}
    report.append("%-44s calls=%-2d tools run=%-2d click=%-3s ledger=%s tokens (%s)" % (
        label, len(run["requests"]), len(run["tools_executed"]),
        "YES" if "click" in run["tools_executed"] else "no",
        format(ledger.get("total_tokens", 0), ","), run["result"].ending or "natural"))


# ---------------------------------------------------------------------------
# C. control -- the replay must reproduce the incident first
# ---------------------------------------------------------------------------
c = replay(Env(**INTERACTIVE), real_read_screen=False)
line("C control: no guards (interactive)", c)
check("C: the recording reproduces the incident's shape -- more than 8 model calls",
      len(c["requests"]) > 8, len(c["requests"]))
check("C: ...the unguarded run reaches the click on the taskbar row",
      "click" in c["tools_executed"], c["tool_names"])
check("C: ...and used all of the original tool steps",
      c["tool_names"][:2] == ["read_screen", "take_screenshot"] and "list_windows" in c["tool_names"],
      c["tool_names"])
check("C: an interactive ask is never limited (the ledger counts, with no limit)",
      (c["result"].usage_total or {}).get("limit") is None
      and (c["result"].usage_total or {}).get("total_tokens", 0) > 20000,
      c["result"].usage_total)
# The measured total of the recording, thinking included (RC5: ~36.3K)
check("C: the ledger's whole-ask total includes the hidden thinking tokens",
      (c["result"].usage_total or {}).get("thinking_tokens") == 4109
      and (c["result"].usage_total or {}).get("total_tokens", 0) > 36000,
      c["result"].usage_total)

# ---------------------------------------------------------------------------
# A. unattended, not approved for the desktop
# ---------------------------------------------------------------------------
a = replay(Env(**unattended("")), real_read_screen=True)
line("A unattended, desktop NOT approved", a)
check("A: ends in at most 2 model calls", len(a["requests"]) <= 2, len(a["requests"]))
check("A: no tool ran, so no click", not a["tools_executed"] and "click" not in a["tools_started"],
      (a["tools_executed"], a["tools_started"]))
check("A: the reply is a single plain line, not a question to nobody",
      bool(a["result"].ok and a["result"].text and "\n" not in a["result"].text.strip()
           and "?" not in a["result"].text), a["result"].text)

# ---------------------------------------------------------------------------
# B. unattended, approved for the desktop, no OCR, no vision route
# ---------------------------------------------------------------------------
# First, without any model at all: what does the real read_screen say?
direct = None
saved_ocr = (ocr_tools.pytesseract, ocr_tools.Image)
ocr_tools.pytesseract = None
ocr_tools.Image = None
try:
    with Env(**unattended("desktop")):
        # vision_enabled=false is in the config the replay writes; write it now too
        cfg0 = json.loads(json.dumps(ai_config.DEFAULT_AI_CONFIG))
        cfg0["providers"] = json.loads(json.dumps(REC["providers"]))
        cfg0["defaults"]["vision_enabled"] = False
        ai_config.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        ai_config.AI_CONFIG_FILE.write_text(json.dumps(cfg0), encoding=ai_config.ENCODING)
        direct = ocr_tools.tool_read_screen({})
finally:
    ocr_tools.pytesseract, ocr_tools.Image = saved_ocr
check("B: with no OCR and no vision, read_screen fails once, before capturing, and says so",
      isinstance(direct, dict) and direct.get("error") and direct.get("can_see_screen") is False,
      direct)
check("B: ...and marks the failure non-retryable (the circuit breaker's trigger)",
      isinstance(direct, dict) and direct.get("retryable") is False, direct)

b = replay(Env(**unattended("desktop")), real_read_screen=ocr_tools.tool_read_screen)
line("B unattended, desktop approved, no OCR/vision", b)
check("B: ends in at most 2 model calls", len(b["requests"]) <= 2, len(b["requests"]))
check("B: only read_screen was started; no screenshot, no click",
      b["tools_started"] == ["read_screen"] and "click" not in b["tools_started"], b["tools_started"])
check("B: the reply is a single plain line, not a question to nobody",
      bool(b["result"].ok and b["result"].text and "\n" not in b["result"].text.strip()
           and "?" not in b["result"].text), b["result"].text)

# ---------------------------------------------------------------------------
# D. unattended, nothing stops it but the token budget
# ---------------------------------------------------------------------------
# The default limit (30,000). The recording's running total, thinking included,
# is 2,486 / 5,366 / 8,669 / 13,748 / 19,866 / 26,766 / 33,833 after each
# answered gemini round -- the 7th (33,833) is the one that asks for the click.
d_default = replay(Env(**unattended("desktop")), real_read_screen=False)
line("D default limit (30,000), tools as recorded", d_default)
ledger = d_default["result"].usage_total or {}
check("D: the default limit is 30,000 and applies to a scheduled run",
      ledger.get("limit") == 30000, ledger)
check("D: the run was stopped for cost, not left to finish",
      d_default["result"].ending == "token_budget" and ledger.get("exceeded") is True,
      (d_default["result"].ending, ledger))
check("D: the click that the over-limit response asked for did NOT run",
      "click" not in d_default["tools_executed"], d_default["tools_executed"])
check("D: no model call was made after the one that crossed the limit",
      len(d_default["requests"]) < len(c["requests"])
      and d_default["requests"][-1]["tools"] is True
      and ledger.get("total_tokens") == 33833,   # exactly the running total at that response
      (len(d_default["requests"]), ledger.get("total_tokens")))
check("D: the stop reply names the cause, what ran, and the setting to change",
      "over its limit of 30,000" in (d_default["result"].text or "")
      and "defaults.scheduled_token_budget" in (d_default["result"].text or "")
      and "read_screen" in (d_default["result"].text or ""), d_default["result"].text)
check("D: nothing is offered to 'go ahead' with after a cost stop",
      "go ahead" not in (d_default["result"].text or "").lower(), d_default["result"].text)

d_small = replay(Env(**unattended("desktop", "12000")),
                 real_read_screen=False)
line("D limit 12,000 (JARVIS_TOKEN_BUDGET)", d_small)
check("D: a lower limit stops it sooner (fewer calls, fewer tools)",
      len(d_small["requests"]) < len(d_default["requests"])
      and len(d_small["tools_executed"]) < len(d_default["tools_executed"]),
      (len(d_small["requests"]), len(d_default["requests"])))
check("D: JARVIS_TOKEN_BUDGET overrides the config default",
      (d_small["result"].usage_total or {}).get("limit") == 12000, d_small["result"].usage_total)

d_off = replay(Env(**unattended("desktop", "0")),
               real_read_screen=False)
line("D limit off (JARVIS_TOKEN_BUDGET=0)", d_off)
check("D: 0 turns the limit off -- the run goes as far as the recording does",
      (d_off["result"].usage_total or {}).get("limit") is None and d_off["result"].ending != "token_budget",
      d_off["result"].usage_total)

# ---------------------------------------------------------------------------
# The measured figures
# ---------------------------------------------------------------------------
print("\nMeasured on the real ask() loop (recorded model replies; the closing call is synthetic):")
for r in report:
    print("  " + r)
for label, run in (("A", a), ("B", b)):
    ledger_total, est_synth = real_tokens(run)
    print("  %s: ledger %s tokens, of which the closing call is ~%s (estimated from its request size; "
          "recording ratio %.2f chars/token)" % (label, format(ledger_total, ","), format(est_synth, ","),
                                                 REC["chars_per_token"]))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
