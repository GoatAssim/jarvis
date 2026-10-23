"""L.24 T1 regression: the real "has anyone texted you on discord" incident
(`5267d91a8b996d48`, 2026-09-30) replayed through the REAL ask() loop, offline.

What the incident was. One Gemini ask, five model rounds, 10,649 input tokens,
for a question recent_dms answers in two. The router offered the 19-tool desktop
group (click, type_text, take_screenshot...) and not `recent_dms`, so the model
walked the desktop: list_windows (22 titles back), focus_window, read_screen
(a Tesseract error), take_screenshot -- and answered from a window title.
The log's per-round input tokens: 1,435 -> 1,947 -> 2,057 -> 2,483 -> 2,727.

What is real and what is not (read this before trusting a number):

  RECORDED   the fixture itself (tests/fixtures/5267d91a8b996d48.jsonl, scrubbed:
             user name, Discord handle and window titles replaced; opaque
             thoughtSignature blobs stubbed). Part 1 asserts what it says.
  REAL       tool_router.route() and ai_client.ask() with the real Gemini
             adapter, the real prompt, the real tool offering.
  SYNTHETIC  every model reply in Part 2. The recorded model replies CANNOT be
             replayed against the current tree: they call list_windows, which
             the router no longer offers for this prompt, so serving them would
             prove nothing. Part 2 instead gives the model the one sensible
             reply (call recent_dms, then answer) and measures what the CURRENT
             tree sends. It therefore proves "the tool offering and the prompt
             are small enough for a 2-round answer", NOT "the model will pick
             recent_dms" -- that is the model's behaviour and is not tested.
  ESTIMATED  Part 2's input tokens: each request's JSON size divided by the
             chars-per-token the recording itself shows (first request:
             6,026 chars = 1,435 tokens). Every figure derived from it says so.
  STUBBED    system_tools.execute_tool: this file can never touch Discord, the
             desktop or anything else. The recent_dms result is made up
             ("no new DMs"), and is small: a real one with many DMs is larger.

Run: python3 tests/test_l24_5267_replay.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import (ai_client, ai_config, ai_providers, conversations, key_health,  # noqa: E402
                    tool_router)
from jarvis import tools as system_tools  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "5267d91a8b996d48.jsonl"
if not FIXTURE.exists():   # tests/fixtures is kept out of the shared copy (private conversations)
    print("SKIPPED: tests/fixtures/5267d91a8b996d48.jsonl is not in this copy")
    sys.exit(0)
CEILING = 2500          # the L.24 target for this ask: total ESTIMATED input tokens
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def tool_names(payload):
    return [f["name"] for group in (payload.get("tools") or [])
            for f in group.get("function_declarations", [])]


# ---------------------------------------------------------------------------
# Part 1 -- what the recording says
# ---------------------------------------------------------------------------
rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
raw = FIXTURE.read_text(encoding="utf-8")
requests = [r["data"]["payload"] for r in rows if r["direction"] == "request"]
usage = [r["data"] for r in rows if r["direction"] == "usage"]
calls = [r["data"]["name"] for r in rows if r["direction"] == "tool_call"]
USER_TEXT = requests[0]["contents"][0]["parts"][0]["text"]

check("fixture: scrubbed (no user name, no Discord handle, no raw signature blobs)",
      "assim" not in raw and "Bac2027" not in raw and '"thoughtSignature":"E' not in raw)
check("fixture: the user text is the incident's", USER_TEXT == "hey jarvis has anyone texted you on discord", USER_TEXT)
check("fixture: five rounds", len(requests) == 5 and len(usage) == 5, (len(requests), len(usage)))
check("fixture: input tokens per round 1435/1947/2057/2483/2727, 10,649 in all",
      [u["input_tokens"] for u in usage] == [1435, 1947, 2057, 2483, 2727]
      and sum(u["input_tokens"] for u in usage) == 10649, [u["input_tokens"] for u in usage])
check("fixture: the model walked the desktop", calls == ["list_windows", "focus_window", "read_screen", "take_screenshot"], calls)
offered = tool_names(requests[0])
check("fixture: the first request offered the 19-tool desktop group and NOT recent_dms",
      len(offered) == 19 and "click" in offered and "take_screenshot" in offered and "recent_dms" not in offered,
      offered)
CHARS_PER_TOKEN = len(json.dumps(requests[0])) / float(usage[0]["input_tokens"])
check("fixture: the chars-per-token ratio the estimate uses is sane (3 to 6)", 3.0 < CHARS_PER_TOKEN < 6.0, CHARS_PER_TOKEN)
check("fixture: the recorded run grew every round (the cost was the re-sent history)",
      all(b > a for a, b in zip([u["input_tokens"] for u in usage], [u["input_tokens"] for u in usage][1:])))

# ---------------------------------------------------------------------------
# Part 2 -- the same words through the current tree
# ---------------------------------------------------------------------------
route = tool_router.route(USER_TEXT)
check("router: the prompt now routes to the channels group", "channels" in route.groups, route.groups)
check("router: and not to the desktop group", "desktop" not in route.groups, route.groups)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text, self.headers = json.dumps(body), {}

    def json(self):
        return self._body


def _gemini(parts, tokens):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": tokens, "candidatesTokenCount": 12}}


def replay():
    try:
        key_health.HEALTH_FILE.unlink()
    except OSError:
        pass
    cfg = json.loads(json.dumps(ai_config.DEFAULT_AI_CONFIG))
    cfg["providers"] = [{"name": "gemini", "type": "gemini", "enabled": True,
                         "model": "gemini-3.6-flash", "api_keys": ["replay-placeholder-1"]}]
    d = cfg["defaults"]
    d["provider_priority"] = ["gemini"]
    d["prompt_mode"] = "compact"            # the recording's own mode ("100% Capacity")
    d["min_round_interval_seconds"] = 0
    ai_config.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    ai_config.AI_CONFIG_FILE.write_text(json.dumps(cfg), encoding=ai_config.ENCODING)
    conv_id = conversations.new_conversation(make_current=False)
    seen, executed = [], []

    def fake_post_json(url, headers, payload, timeout):
        est = int(round(len(json.dumps(payload)) / CHARS_PER_TOKEN))
        seen.append({"tools": tool_names(payload), "est_input": est, "payload": payload})
        if len(seen) == 1:        # SYNTHETIC: the one sensible first reply
            return _Resp(200, _gemini([{"functionCall": {"name": "recent_dms", "args": {}}}], est)), None
        return _Resp(200, _gemini([{"text": "No new messages on Discord, sir."}], est)), None

    def fake_execute_tool(name, arguments=None, verbosity=None, context=None):
        executed.append(name)     # STUBBED, and small: "nothing new"
        return {"ok": True, "count": 0, "messages": [], "note": "No recent direct messages."}

    saved = (ai_providers._post_json, system_tools.execute_tool, time.sleep)
    ai_providers._post_json, system_tools.execute_tool, time.sleep = fake_post_json, fake_execute_tool, (lambda s: None)
    for k in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_TOKEN_BUDGET"):
        os.environ.pop(k, None)
    try:
        result = ai_client.ask(USER_TEXT, commands=[], conversation_id=conv_id,
                               on_confirm_request=lambda n, a, risk_note=None: True)
    finally:
        ai_providers._post_json, system_tools.execute_tool, time.sleep = saved
    return result, seen, executed


result, seen, executed = replay()
first = seen[0]["tools"] if seen else []
check("now: the first request offers recent_dms", "recent_dms" in first, first)
check("now: it offers none of the desktop tools the incident walked",
      not ({"click", "take_screenshot", "focus_window", "read_screen", "list_windows", "type_text"} & set(first)), first)
check("now: it offers far fewer tools than the incident's 19 (the channels group is small)", len(first) < 19, len(first))
check("now: two requests (call the tool, then answer), not five", len(seen) == 2, len(seen))
check("now: the one tool that ran is recent_dms", executed == ["recent_dms"], executed)
check("now: the ask succeeds and answers", result.ok and "Discord" in (result.text or ""), (result.ok, result.text))
est = [s["est_input"] for s in seen]
total = sum(est)
print(f"         estimated input tokens per request {est}, total {total}  (recorded incident: 10,649)")
check(f"now: ESTIMATED total input is at most {CEILING} (the L.24 target)", total <= CEILING, (est, total))
check("now: and well under the recorded incident's 10,649", total < 10649 / 3, total)
check("now: the second request is not bigger than the first plus a small tool result",
      len(est) == 2 and est[1] - est[0] < 400, est)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
